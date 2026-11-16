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

    async def match(self, *, email: str = "", first_name: str = "",
                    last_name: str = "", domain: str = "",
                    organization_name: str = "") -> dict | None:
        """Find one person from what you already know about them.

        The same ``people/match`` endpoint ``enrich`` uses, addressed by email
        or by name plus company instead of by Apollo id — which is how a list
        that came from outside Apollo becomes a real contact. One credit when
        Apollo finds something, none when it doesn't.
        """
        payload = {"reveal_personal_emails": False, "reveal_phone_number": False}
        if email:
            payload["email"] = email
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if domain:
            payload["domain"] = domain
        if organization_name:
            payload["organization_name"] = organization_name
        if not (email or (first_name and (domain or organization_name))):
            return None
        data = await self._post("people/match", payload)
        person = data.get("person")
        if not person or person.get("match_confidence") == "none":
            return None
        return person

    async def bulk_match(self, people: list[dict]) -> list[dict | None]:
        """Up to ten people in one call, billed exactly as ten single matches.

        Same credits, a tenth of the round trips. Returns one entry per input,
        in order, with None where Apollo found nobody.
        """
        if not people:
            return []
        details = [{k: v for k, v in p.items() if v} for p in people[:10]]
        data = await self._post("people/bulk_match", {
            "details": details,
            "reveal_personal_emails": False,
            "reveal_phone_number": False,
        })
        matches = data.get("matches") or []
        out: list[dict | None] = []
        for i in range(len(details)):
            person = matches[i] if i < len(matches) else None
            if not person or person.get("match_confidence") == "none":
                out.append(None)
            else:
                out.append(person)
        return out

    async def job_postings(self, organization_id: str, page: int = 1) -> list[dict]:
        """A company's open roles, from Apollo rather than from its ATS board.

        Paid plans only. One credit a page. The ATS collector
        (sellbird/collectors/ats.py) reads Greenhouse, Lever, Ashby and
        Workable for free; this covers every company that uses none of them.
        """
        if not organization_id:
            return []
        r = await self._client.get(
            f"{API}/organizations/{organization_id}/job_postings",
            params={"page": page})
        if r.status_code != 200:
            raise ApolloError(f"Apollo job_postings returned {r.status_code}: "
                              f"{r.text[:300]}")
        return (r.json() or {}).get("organization_job_postings") or []

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
