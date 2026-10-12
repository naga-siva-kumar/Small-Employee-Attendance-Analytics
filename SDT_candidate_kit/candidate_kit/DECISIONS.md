# DECISIONS.md 

1. **Indexes.** I created a unique index on `employees.emp_code` (enforces no-duplicate creates atomically), a unique compound index on `attendance_logs(emp_code, date)` (enforces one record per employee per day and prevents concurrent duplicate punch-ins), a compound index on `attendance_logs(date desc, emp_code asc)` (serves the list-attendance sort and analytics date-range queries), and indexes on `employees.department` and `employees.joined_on` for look-ups and headcount filters. I considered a separate index on `attendance_logs.status` but rejected it because status is low-cardinality and the date+emp_code indexes cover the filtered queries well enough.

2. **Punch-in race.** Both requests hit `insert_one` against the unique `(emp_code, date)` index. MongoDB guarantees exactly one insert succeeds; the other throws `DuplicateKeyError`, which my code catches and returns 409. No `find_one` check precedes the insert, so there is no window for a race.

3. **Ties.** I use MongoDB's `$setWindowFields` with `$rank`, which implements standard competition ranking — tied employees share the same rank and the next rank is skipped. The `$match` stage filters `rank <= limit` *after* ranking, so all tied employees at the cutoff rank are included even if that exceeds `limit` rows. Final sort is `total_late_minutes` desc, `emp_code` asc.

4. **Headcount.** The department summary first queries the `employees` collection for all employees with `joined_on <= last_day_of_month`, grouped by department. This gives headcount independently of whether those employees have any attendance logs. The aggregation pipeline on `attendance_logs` only computes metrics; headcount comes from the employees query.

5. **One thing I would change.** I would shard `attendance_logs` by `emp_code` (or a hashed key) to distribute the write and read load, and move the analytics aggregations to read from secondary replicas to avoid contention with real-time punch operations.
