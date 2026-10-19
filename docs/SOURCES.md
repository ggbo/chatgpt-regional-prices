# Sources and evidence boundaries

Checked on 2026-10-19 (Asia/Shanghai). Source dates below are the dates shown by the source, not inferred publication dates.

## Primary official sources

### OpenAI support status

- [ChatGPT Supported Countries](https://help.openai.com/en/articles/7947663-chatgpt-supported-countries) is the authority used for `config/support-status.json`. The page says it lists countries and territories from which OpenAI currently supports ChatGPT access on web and mobile. When checked, it displayed the relative label “Updated: 2 months ago”; it did not expose an absolute update date in the rendered article.
- The English names were mapped to ISO 3166-1 alpha-2 codes in the versioned catalog. The official list mapped to 208 catalog codes. Forty-one ISO catalog entries were absent and are recorded as `unsupported`; XK, EU, and US2 are `unknown` because the article does not directly resolve those non-standard or auxiliary targets.
- This status does not prove that a pricing config exists or that a specific account can buy a plan. The collector models those facts separately.

### OpenAI web price configuration

- The observed configuration route is `https://chatgpt.com/backend-api/checkout_pricing_config/configs/{CONFIG_ID}`. Each reference Web identity links to the concrete official URL for its target.
- This is a publicly readable route observed on an OpenAI domain. No public stability guarantee or formal field contract was found, so it must be treated as an implementation source with structural validation, not a guaranteed public API.
- A response must match the requested country/config identity before its prices are attributed to that target. Reference presence never substitutes for a successful current official fetch.

### Official store applications

- Apple source links use the official ChatGPT app identifier `6448311069`, localized by storefront, for example [US App Store](https://apps.apple.com/us/app/chatgpt/id6448311069).
- Google Play source links use the official package `com.openai.chatgpt`, for example [Google Play](https://play.google.com/store/apps/details?id=com.openai.chatgpt).
- The PriceAI pages label Google Play entries as inferred. The baseline therefore records them as `unverified_range`; they are identities for coverage reconciliation, not verified plan prices.

## Supplemental reference baseline

PriceAI is used only to establish a dated comparison set of region/channel/plan identities. Its monetary values are not imported into this project.

| Plan | Detail page | Source date | Regions | Identities | Channel breakdown |
| --- | --- | --- | ---: | ---: | --- |
| Go | [PriceAI Go](https://priceai.cc/official-prices/chatgpt__go-monthly) | 2026-10-13 | 170 | 235 | Web 151, iOS 47, Google Play inferred 37 |
| Plus | [PriceAI Plus](https://priceai.cc/official-prices/chatgpt__plus-monthly) | 2026-10-13 | 69 | 106 | Web 59, iOS 47 |
| Pro 100 | [PriceAI Pro 100](https://priceai.cc/official-prices/chatgpt__pro-5x) | 2026-10-13 | 64 | 98 | Web 51, iOS 47 |
| Pro 200 | [PriceAI Pro 200](https://priceai.cc/official-prices/chatgpt__pro-20x) | 2026-10-13 | 70 | 146 | Web 60, iOS 47, Google Play inferred 39 |
| Pro 500 | [PriceAI Pro 500](https://priceai.cc/official-prices/chatgpt__pro-500) | 2026-10-13 | 77 | 112 | Web 65, iOS 47 |

The five pages yielded 697 unique plan/region/channel identities across 178 region IDs. Every extracted target ID maps to `config/regions.json`; this is recorded as zero unmapped identities. Counts are evidence about these dated reference pages, not an official coverage ceiling and not this project's completion target.

Extraction used stable row identity keys in the rendered Next.js HTML. `config/reference-coverage.json` stores the SHA-256 of each fetched HTML artifact, the reported counts, extracted counts, channel counts, and completeness verdict. These hashes identify the local extraction artifacts; they are not claimed to be hashes of an upstream API response or permanent PriceAI content identifiers.

## Catalog source

- `config/regions.json` identifies [pycountry](https://github.com/pycountry/pycountry) and the ISO Online Browsing Platform as its catalog provenance. The catalog has 249 ISO entries, XK, and the auxiliary targets EU and US2.
- The project scans the catalog independently of both the support list and PriceAI. A target remains searchable even when it has no valid quote.

## Reconciliation rules

`scripts/reconcile_reference.py` compares the checked-in identity baseline with the current coverage matrix. It distinguishes current matches, historical matches, request failures, missing plan fields, unattempted targets, inferred references, out-of-scope store channels, official discoveries absent from the reference baseline, catalog mismatches, and special configurations.

The comparison does not copy third-party prices, does not turn store ranges into plan prices, and does not treat a similar total count as proof of coverage.
