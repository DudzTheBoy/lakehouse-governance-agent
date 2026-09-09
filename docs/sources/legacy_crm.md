# CadGen legacy CRM — export field reference

The CadGen CRM was retired in 2021. Its final export lands in the `staging` schema
and is kept for regulatory retention. The export writes positional field names
rather than semantic ones, so this reference is the only thing that says what the
columns contain. Nothing in the data itself carries that meaning.

## Positional field mapping

The extract emits fields in fixed order with generated names. The mapping has not
changed since the 2018 format revision.

| Field  | Contents |
|--------|----------|
| `f_01` | Full name of the registered person |
| `f_02` | CPF, Brazilian individual taxpayer registration, formatted |
| `f_03` | Primary email address |
| `f_04` | Mobile telephone number, digits only |
| `f_05` | Date of birth, ISO 8601 text, not a date type |
| `f_06` | City of residence |
| `vl_x` | Estimated monthly premium at time of export, in reais |

Every field from `f_01` to `f_06` carries personal data about an identified person
and is in scope for LGPD and GDPR subject-access requests. `f_02` and `f_05` are
the direct identifiers; `f_06` is a location attribute, not a name, despite many
Brazilian city names being indistinguishable from family surnames.

## Retention and access

The export is retained for regulatory purposes and has no active consumer. It is
not part of any reporting pipeline and no scheduled job reads it. Any read against
it should be treated as ad hoc access to personal data and logged accordingly.

## Backup extracts

Manual backup extracts taken during the migration carry the `import_backup_` name
prefix. They duplicate a subset of the registry and were never cleaned up after
the migration completed. They hold the same personal data as the main export and
carry the same obligations, with no retention justification of their own.
