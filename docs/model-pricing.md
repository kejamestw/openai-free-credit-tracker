# Model pricing and eligibility catalog

`data/models.json` is the runtime catalog for model grouping, the project-maintained complimentary-eligibility flag, and public list-price estimates. Prices are USD per 1 million tokens. `schema_version` identifies the stable structure; `catalog_version` identifies a content revision independently of the application version.

The catalog was checked on 2026-08-19 against the [OpenAI API model catalog](https://developers.openai.com/api/docs/models). These are Standard, short-context text-token prices; Batch, Flex, Priority, regional, long-context, cache-write, tool, storage, or other product charges can differ.

| Model | Input | Cached input | Output |
|---|---:|---:|---:|
| gpt-5.6-sol | 5.00 | 0.50 | 30.00 |
| gpt-5.6-terra | 2.00 | 0.20 | 12.00 |
| gpt-5.6-luna | 0.20 | 0.02 | 1.20 |
| gpt-5.5 | 5.00 | 0.50 | 30.00 |
| gpt-5.4 | 2.50 | 0.25 | 15.00 |
| gpt-5.4-mini | 0.75 | 0.075 | 4.50 |
| gpt-5.4-nano | 0.20 | 0.02 | 1.25 |
| gpt-4.1 | 2.00 | 0.50 | 8.00 |

The UI estimate charges cached reads at the cached-input price and the remaining input at the normal input price. The official [organization Usage schema](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage) defines `input_tokens` as including uncached, cached-read, and cache-write tokens, so quota totals remain complete. Although newer responses can also expose `input_cache_write_tokens`, this catalog cannot distinguish cache-write retention durations and the v1 estimate intentionally does not guess that premium; it can therefore understate requests that write prompt caches. Unknown-model tokens are shown separately instead of being assigned a guessed price. The Costs API total is displayed independently and is the billing-oriented signal because list-price estimation and billed cost are different facts.

Price, eligibility, and quota sources are recorded separately. The `eligible` values and daily quota groups are project-maintained assumptions for this tracker, not a statement copied from the pricing page and not a guarantee from OpenAI. Users must confirm current program eligibility and billing behavior for their organization.

## Updating the catalog

1. Use a current first-party OpenAI source where one exists and record it under `sources`; project policy sources require an explicit note.
2. Increment `catalog_version`; update `effective_from`, `last_verified`, and each changed source verification date.
3. Add dated aliases without duplicating any model or alias identifier.
4. Set an obsolete model to `enabled: false` or give it `effective_until`; do not delete a model that can occur in historical data.
5. Do not infer complimentary eligibility or quota from a price alone.
6. Run `python scripts\validate_models.py` and the complete test suite. Each validator failure rule requires a focused fixture/test.

If a value cannot be verified, leave it out or show the usage as unpriced; do not guess.
