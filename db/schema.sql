-- Schema for the cold-storage over-temperature alarm archive rebuild.
--
-- The alarm parent table is range-partitioned by UTC calendar month.  A DEFAULT
-- partition catches every late alarm until the month's historical batch is
-- attached.  Attaching a batch moves the month's rows out of the default
-- partition into a dedicated month partition.

CREATE TABLE IF NOT EXISTS alarm (
    alarm_no       text        NOT NULL,
    occurred_at    timestamptz NOT NULL,
    warehouse_no   text        NOT NULL,
    temperature    numeric     NOT NULL,
    -- Denormalised UTC month.  RANGE partition keys must be plain columns and
    -- PostgreSQL cannot use a generated column as a partition key, so the
    -- application fills this in; the CHECK constraint below rejects rows whose
    -- occurred_month disagrees with occurred_at.
    occurred_month date        NOT NULL,
    source         text        NOT NULL DEFAULT 'late'
                               CHECK (source IN ('late', 'history')),
    batch_id       bigint,
    CONSTRAINT alarm_occurred_month_matches CHECK (
        occurred_month = (date_trunc('month', occurred_at AT TIME ZONE 'UTC'))::date
    ),
    -- Unique per partition.  Because occurred_month is the partition key, this
    -- also means "one row per alarm number per UTC month" across the parent.
    PRIMARY KEY (occurred_month, alarm_no)
) PARTITION BY RANGE (occurred_month);

-- Late alarms and anything that does not have a month partition land here.
CREATE TABLE IF NOT EXISTS alarm_default PARTITION OF alarm DEFAULT;

-- One row per upload batch.  A batch is only "attached" after validation and a
-- successful ALTER TABLE ... ATTACH PARTITION.  Rejected batches are kept for
-- auditing together with their conflict report.  A same-month re-attach marks
-- the previously attached batch(es) as "superseded".
CREATE TABLE IF NOT EXISTS alarm_batch (
    id             bigserial   PRIMARY KEY,
    month_start    date        NOT NULL,
    month_end      date        NOT NULL,
    filename       text        NOT NULL,
    status         text        NOT NULL DEFAULT 'staged'
                               CHECK (status IN ('staged', 'rejected', 'attached', 'superseded')),
    row_count      integer     NOT NULL DEFAULT 0,
    rejected_count integer     NOT NULL DEFAULT 0,
    conflict_json  jsonb,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    attached_at    timestamptz
);

-- Migration for databases created before same-month re-attach existed: widen
-- the status CHECK constraint to allow "superseded".  Idempotent.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'alarm_batch_status_check'
          AND pg_get_constraintdef(oid) NOT LIKE '%superseded%'
    ) THEN
        ALTER TABLE alarm_batch
            DROP CONSTRAINT alarm_batch_status_check;
        ALTER TABLE alarm_batch
            ADD CONSTRAINT alarm_batch_status_check
            CHECK (status IN ('staged', 'rejected', 'attached', 'superseded'));
    END IF;
END $$;

-- Staging area for a batch's CSV rows.  Rows are loaded here first so that a
-- failed attach never touches the parent table.
CREATE TABLE IF NOT EXISTS alarm_staging (
    batch_id      bigint      NOT NULL REFERENCES alarm_batch(id) ON DELETE CASCADE,
    line_no       integer     NOT NULL,
    alarm_no      text        NOT NULL,
    occurred_at   timestamptz NOT NULL,
    warehouse_no  text        NOT NULL,
    temperature   numeric     NOT NULL,
    PRIMARY KEY (batch_id, line_no)
);
