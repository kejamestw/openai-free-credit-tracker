# Model pricing and eligibility catalog

`data/models.json` is the runtime catalog for model grouping, the project-maintained complimentary-eligibility flag, and public list-price estimates. Prices are USD per 1 million tokens.

The catalog was checked on 2026-07-31 against the [OpenAI API pricing page](https://developers.openai.com/api/docs/pricing). These are Standard, short-context text-token prices; Batch, Flex, Priority, regional, long-context, tool, storage, or other product charges can differ.

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

The UI estimate charges cached input at the cached-input price and the remaining input at the normal input price. It shows unknown-model tokens separately instead of guessing a price. The Costs API total is displayed independently because list-price estimation and billed cost are different signals.

The `eligible` values and daily quota groups are project-maintained assumptions for this tracker, not a statement copied from the pricing page and not a guarantee from OpenAI. Users must confirm current program eligibility and billing behavior for their organization.

## Updating the catalog

1. Use a current first-party OpenAI source and record its URL.
2. Update `last_updated` with the actual verification date.
3. Add dated aliases without duplicating any model or alias identifier.
4. Do not infer complimentary eligibility from a price alone.
5. Run `python scripts\validate_models.py` and the complete test suite.

If a value cannot be verified, leave it out or show the usage as unpriced; do not guess.
