### SQL and migrations

- Destructive steps (`DROP TABLE`, `DROP COLUMN`, `TRUNCATE`, and `DELETE` or `UPDATE` without `WHERE`) with no backup or staged rollout.
- Locking: adding a column with a default, or adding an index without `CONCURRENTLY` (Postgres), on a big table can lock it for a long time.
- A migration that the currently deployed app version can't run against (deploy order). Not reversible when it should be.
- `NOT NULL` added to a column that already has nulls. A changed type that truncates data.
- A new query pattern with no supporting index.
