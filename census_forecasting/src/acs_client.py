"""ACS public API client with on-disk JSON cache.

Why caching matters here
------------------------
The back-test fires N anchor years × M indicators × K geographies of
ACS calls. Census serves these reliably but slowly; without a cache,
every test run hits the network. We persist responses keyed by
(year, vintage, indicator, geographic-scope) so that:

1. Reruns are deterministic — the same point estimates and MOEs are
   used every time a test runs, which matters for the unit tests that
   pin numerical projections.
2. The package works offline once the cache is warm.
3. Reviewers can git-clone, run the tests, and get the same numbers
   without an API key or network access.

The cache is a single JSON file. ACS requests are tiny (a few KB each);
no need for a more complex store.

API reference
-------------
https://www.census.gov/data/developers/data-sets/acs-1year.html
https://www.census.gov/data/developers/data-sets/acs-5year.html
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from typing import Optional

from .models import AcsObservation


_PKG_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_PATH = _PKG_ROOT / "data" / "acs_cache.json"

ACS_BASE = "https://api.census.gov/data"

# Vintages where 1-year ACS was suspended or unreliable. The 2020 1-year
# release was *not* published in the standard form due to COVID data-quality
# issues; Census issued an experimental "ACS-X" set instead which we do not
# consume. Treat 2020 as missing for 1-year and let the model interpolate
# (or skip) accordingly.
SUSPENDED_ONE_YEAR = frozenset({2020})


class AcsClient:
    """ACS Census API client with file-backed cache.

    Pass `api_key=None` (the default) to use unauthenticated access — the
    Census API allows ~500 calls/IP/day without a key, which is plenty for
    this package's needs. If you set `CENSUS_API_KEY` in your environment
    or pass an explicit `api_key`, it's appended to every request.
    """

    def __init__(
        self,
        cache_path: Path = DEFAULT_CACHE_PATH,
        api_key: Optional[str] = None,
        offline: bool = False,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.api_key = api_key or os.environ.get("CENSUS_API_KEY")
        self.offline = offline
        self._cache: dict = self._load_cache()

    def _load_cache(self) -> dict:
        if not self.cache_path.exists():
            return {}
        try:
            with open(self.cache_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[acs_client] cache load failed ({exc}); starting fresh", file=sys.stderr)
            return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self._cache, f, indent=1, sort_keys=True)
        tmp.replace(self.cache_path)

    @staticmethod
    def _cache_key(year: int, vintage: str, indicators: tuple[str, ...], geo_scope: str) -> str:
        return f"{vintage}|{year}|{geo_scope}|{','.join(sorted(indicators))}"

    def _fetch_url(self, url: str) -> list[list]:
        if self.offline:
            raise RuntimeError(f"offline mode: refusing network call to {url}")
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # 204 = "no data for this combination", which Census uses for
            # vintages a survey didn't run (e.g. 2020 1-year). Surface as
            # an empty result rather than an exception so callers can fall
            # through to the next vintage.
            if exc.code == 204:
                return []
            raise
        return json.loads(body)

    def fetch_table(
        self,
        year: int,
        vintage: str,
        indicators: tuple[str, ...],
        geo_scope: str,
    ) -> list[dict]:
        """Fetch one ACS table call, with cache.

        Parameters
        ----------
        year : int
            Calendar year. For 5-year vintages this is the *end year*
            (e.g. 2024 → 2020-2024 estimates).
        vintage : "1y" | "5y"
        indicators : tuple of column IDs (e.g. ("B19013_001E", "B19013_001M"))
        geo_scope : URL fragment, e.g. "for=county:*&in=state:15"
            for all Hawaii counties.

        Returns
        -------
        List of dict rows (header → value), one per geography.
        """
        if vintage not in ("1y", "5y"):
            raise ValueError(f"vintage must be '1y' or '5y', got {vintage!r}")
        if vintage == "1y" and year in SUSPENDED_ONE_YEAR:
            return []

        key = self._cache_key(year, vintage, indicators, geo_scope)
        if key in self._cache:
            return self._cache[key]

        endpoint = "acs1" if vintage == "1y" else "acs5"
        get_clause = ",".join(("NAME",) + tuple(indicators))
        url = f"{ACS_BASE}/{year}/acs/{endpoint}?get={get_clause}&{geo_scope}"
        if self.api_key:
            url += f"&key={self.api_key}"

        rows = self._fetch_url(url)
        if not rows:
            self._cache[key] = []
            self._save_cache()
            return []

        header, *data = rows
        out = [dict(zip(header, r)) for r in data]
        self._cache[key] = out
        self._save_cache()
        return out

    def fetch_series(
        self,
        indicator: str,
        years: tuple[int, ...],
        vintage: str,
        state_fips: str,
        county_fips: Optional[str] = None,
    ) -> list[AcsObservation]:
        """Fetch a multi-year time series for one indicator, one geography.

        If `county_fips` is None, returns observations for *all* counties
        in the state (one per year × county). The `geoid` field on each
        AcsObservation is the 5-char state+county FIPS so callers can
        disambiguate.
        """
        moe_indicator = indicator.replace("E", "M") if indicator.endswith("E") else f"{indicator}M"
        if county_fips:
            geo_scope = f"for=county:{county_fips}&in=state:{state_fips}"
        else:
            geo_scope = f"for=county:*&in=state:{state_fips}"

        out: list[AcsObservation] = []
        for year in years:
            rows = self.fetch_table(
                year=year,
                vintage=vintage,
                indicators=(indicator, moe_indicator),
                geo_scope=geo_scope,
            )
            for r in rows:
                geoid = f"{r['state']}{r['county']}"
                est_str = r.get(indicator)
                moe_str = r.get(moe_indicator)
                if est_str is None or est_str in ("", "null"):
                    continue
                try:
                    estimate = float(est_str)
                    moe = float(moe_str) if moe_str not in (None, "", "null") else float("nan")
                except ValueError:
                    continue
                # Census sentinels: estimate of -666666666 / -888888888 etc.
                # are codes for "missing/suppressed/insufficient sample".
                if estimate <= -666666666:
                    continue
                out.append(AcsObservation(
                    estimate=estimate,
                    moe=moe,
                    year=year,
                    vintage=vintage,
                    geoid=geoid,
                    indicator=indicator,
                ))
        return out
