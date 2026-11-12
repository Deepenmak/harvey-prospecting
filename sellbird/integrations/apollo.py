"""Apollo.io: people search (free), org search (free) and enrichment (credits).

Search (``mixed_people/api_search``) costs nothing but returns no email, no
domain and an obfuscated last name. Org search (``mixed_companies/api_search``)
is also free and returns industry, employee count, tech stack and website.
Enrichment (``people/match``) returns the full person record and costs 1 credit
when Apollo finds an email or demographics, 0 when it finds nothing.
So the pattern is: search wide, choose carefully, enrich only the people worth
a credit.

Phones are never requested: a mobile number costs 8 extra credits.
"""

import logging
import os

import httpx

logger = logging.getLogger("sellbird.apollo")

API = "https://api.apollo.io/api/v1"
TIMEOUT = httpx.Timeout(30.0)


def apollo_key() -> str:
    """The Apollo API key from the environment (.env or the cloud env)."""
    for name in ("APOLLO_HARVEY_KEY", "apollo_harvey_key", "APOLLO_API_KEY"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


class ApolloError(RuntimeError):
    pass


class ApolloClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ApolloError("No Apollo key. Set APOLLO_HARVEY_KEY in .env "
                              "or in the cloud environment.")
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json",
                         "Cache-Control": "no-cache"}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=TIMEOUT, headers=self._headers)
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()

    async def _post(self, path: str, payload: dict) -> dict:
        r = await self._client.post(f"{API}/{path}", json=payload)
        if r.status_code != 200:
            raise ApolloError(f"Apollo {path} returned {r.status_code}: {r.text[:300]}")
        return r.json()

    async def search_people(self, filters: dict, page: int = 1,
                            per_page: int = 100) -> dict:
        """Free search. Returns {'total_entries': int, 'people': [...]}."""
        payload = dict(filters, page=page, per_page=min(per_page, 100))
        return await self._post("mixed_people/api_search", payload)

    async def search_organizations(self, filters: dict, page: int = 1,
                                   per_page: int = 100) -> dict:
        """Free org search. Returns {'total_entries': int, 'organizations': [...]}.

        Each org includes: name, website_url, industry, estimated_num_employees,
        primary_domain, technologies (tech stack), latest_funding_stage.
        No credits consumed.
        """
        payload = dict(filters, page=page, per_page=min(per_page, 100))
        return await self._post("mixed_companies/api_search", payload)

    async def enrich_organization(self, domain: str) -> dict | None:
        """Full org record by domain. Credits consumed varies by plan.

        Returns tech stack, headcount growth, funding details, and more.
        Only call for Tier A accounts where the extra signal is worth the cost.
        """
        data = await self._post("organizations/enrich", {"domain": domain})
        return data.get("organization")

    async def enrich(self, person_id: str) -> dict | None:
        """Full record for one person. Costs a credit only if data is found."""
        data = await self._post("people/match", {
            "id": person_id,
            "reveal_personal_emails": False,
            "reveal_phone_number": False,
        })
        person = data.get("person")
        if not person or person.get("match_confidence") == "none":
            return None
        return person
