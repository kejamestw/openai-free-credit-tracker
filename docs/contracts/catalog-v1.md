# Model catalog schema v1

`schema_version` identifies structural compatibility. `catalog_version` identifies a
content revision. `effective_from` and `last_verified` are ISO dates. Price,
eligibility, and quota policy sources are separate records; a pricing page is never
treated as evidence of program eligibility or quota.

Groups have stable IDs, positive quotas, a quota source, and models. Models have a
unique ID/aliases, `enabled`, eligibility, effective range, non-negative input/cached
input/output prices, and separate source references. Historical models are disabled
or given an end date instead of being deleted. Unknown models remain visible as
catalog-unrecognized and are never classified as confirmed free or confirmed priced.

Readers reject unsupported schema versions and unsafe content. If an optional
downloaded catalog is invalid, the bundled last-known-good catalog remains active and
the UI displays a warning. Content changes require first-party source review where
available, validator success, tests, and review of eligibility/quota assumptions.
