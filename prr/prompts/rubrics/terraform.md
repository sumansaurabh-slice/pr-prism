### Terraform and infrastructure

- IAM policies with `"*"` actions or resources where a narrower one works.
- Security groups or firewalls open to `0.0.0.0/0` on non-public ports.
- `ignore_changes` or `prevent_destroy` hiding real drift, and resources that will be destroyed and recreated by this change (check names and keys).
- Missing encryption at rest or in transit, missing backups or retention, and public buckets.
- Required tags missing. Hard-coded account ids, regions or secrets.
- State or provider version changes that affect every module.
