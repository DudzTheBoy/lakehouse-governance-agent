# SeguraCore Policy Administration — integration notes

Documentation for the upstream system that feeds the `raw`, `silver` and `gold`
schemas. This stands in for whatever real system documentation you would drop here:
a vendor's API reference, an internal wiki export, a data contract.

## Customer registry

The customer registry is the system of record for policyholders. One row per
person, keyed by `id_cliente`, which is stable for the lifetime of the customer
relationship and is reused across policy and claim records as a foreign key.

`nome` holds the full legal name as it appears on the policy document.
`cpf` is the Brazilian individual taxpayer registration number, stored formatted
with dots and a dash. It is the natural key for regulatory reporting and is
treated as restricted under LGPD.
`email` and `telefone` are contact channels; `telefone` is a mobile number stored
as digits only, with country and area code, no separators.
`data_nascimento` is used for age-banded pricing and is required for underwriting.
`cidade` and `estado` describe the residence used for regional risk rating.
`data_cadastro` is the date the customer record was opened, not the date of their
first policy.

`apelido` was introduced in 2019 for a customer-portal greeting feature that was
cancelled before launch. The column was never populated and no process writes to
it. It is retained only because removing it requires a coordinated release with
three downstream consumers.

## Policy records

One row per policy, keyed by `id_apolice`, linked to a customer by `id_cliente`.

`tipo_seguro` takes one of four values: `auto`, `residencial`, `vida`, `viagem`.
`valor_premio` is the annualised premium in Brazilian reais, not the monthly
instalment. Reporting that divides it by twelve is a recurring source of error.
`valor_cobertura` is the maximum payout under the policy, also in reais.
`data_inicio` and `data_fim` bound the coverage period; policies are written for
365 days and renewed as new rows rather than by extending an existing row.
`status` is one of `ativa`, `cancelada` or `vencida`. A policy past `data_fim`
that was not renewed becomes `vencida`; `cancelada` means terminated before term,
which triggers a pro-rata refund calculation.

`pais` is a legacy field from a planned international expansion that was never
launched. Every row carries the literal value `Brasil`. It is not a meaningful
dimension and should not be used as a grouping key.

## Claim records

One row per claim, keyed by `id_sinistro`, linked to a policy by `id_apolice`.

`data_ocorrencia` is the date of the insured event, not the date the claim was
filed. The gap between the two is not currently captured.
`tipo_sinistro` classifies the event: `colisao`, `roubo`, `incendio`, `furto` or
`danos_terceiros`.
`valor_sinistro` is the amount claimed, in reais. It is exported as text rather
than a decimal because the extract was originally built against a system that
returned all monetary values as strings. Consumers are expected to cast it. The
fix is tracked but has never been prioritised.
`status_sinistro` follows the claims workflow: `aberto`, `em_analise`, then one of
`aprovado`, `negado` or `pago`.
`descricao` is free text entered by the claims handler and is not validated.

## Derived layers

The `silver` layer holds the curated policy view, filtered to active policies only,
which is why its `status` column carries a single value.

The `gold` layer holds the monthly claims rollup. `taxa_sinistralidade` and related
rate measures are claims value over premium value for the period, expressed as a
ratio and not a percentage.
