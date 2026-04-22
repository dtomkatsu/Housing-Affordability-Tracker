"""Data models for the Hawaii Grocery Price Tracker."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BaselinePrice:
    """A single manually-collected price observation."""

    slot_id: str
    chain: str
    store_id: str
    county: str
    geoid: str
    date: str  # ISO date
    product_name: str
    price: float
    size_qty: float
    size_unit: str
    per_unit_price: float
    is_substitution: bool = False
    substitution_note: str | None = None


@dataclass
class AdjustedPrice:
    """A baseline price adjusted forward by CPI change."""

    slot_id: str
    chain: str
    store_id: str
    county: str
    geoid: str
    baseline_date: str
    adjusted_date: str
    baseline_price: float
    adjusted_price: float
    per_unit_price: float
    cpi_category: str
    cpi_ratio: float  # current_CPI / baseline_CPI


@dataclass
class HouseholdEstimate:
    """Estimated grocery cost for a specific household type and county."""

    household_type: str
    household_label: str
    county: str
    geoid: str
    date: str
    basket_total: float  # full 4-person reference basket
    household_cost: float  # scaled for this household type
    effective_factor: float


@dataclass
class BasketConfig:
    """Loaded basket configuration."""

    items: list[dict]

    @classmethod
    def load(cls, path: Path | None = None) -> BasketConfig:
        if path is None:
            path = Path(__file__).parent.parent / "config" / "basket.json"
        with open(path) as f:
            data = json.load(f)
        return cls(items=data["items"])

    def get_item(self, slot_id: str) -> dict | None:
        for item in self.items:
            if item["slot_id"] == slot_id:
                return item
        return None

    @property
    def slot_ids(self) -> list[str]:
        return [item["slot_id"] for item in self.items]


@dataclass
class StoreConfig:
    """Loaded store configuration."""

    counties: dict
    chains: dict

    @classmethod
    def load(cls, path: Path | None = None) -> StoreConfig:
        if path is None:
            path = Path(__file__).parent.parent / "config" / "stores.json"
        with open(path) as f:
            data = json.load(f)
        return cls(counties=data["counties"], chains=data["chains"])

    def get_geoid(self, county: str) -> str:
        return self.counties[county]["geoid"]

    def all_stores(self) -> list[dict]:
        stores = []
        for chain_id, chain_data in self.chains.items():
            for store in chain_data["stores"]:
                stores.append({**store, "chain": chain_id})
        return stores


@dataclass
class CPIConfig:
    """Loaded CPI series configuration."""

    categories: dict

    @classmethod
    def load(cls, path: Path | None = None) -> CPIConfig:
        if path is None:
            path = Path(__file__).parent.parent / "config" / "cpi_series.json"
        with open(path) as f:
            data = json.load(f)
        return cls(categories=data["categories"])

    def get_series_for_item(self, slot_id: str) -> tuple[str, str]:
        """Return (cpi_category, series_id) for a basket item."""
        for cat_id, cat_data in self.categories.items():
            if slot_id in cat_data["basket_items"]:
                return cat_id, cat_data["series_id"]
        raise ValueError(f"No CPI category found for {slot_id}")

    @property
    def all_series_ids(self) -> list[str]:
        return [cat["series_id"] for cat in self.categories.values()]


@dataclass
class StoreWeightsConfig:
    """Loaded store market share weights per chain per county."""

    weights: dict[str, dict[str, float]]  # county -> chain -> weight
    proxy_chains: dict[str, str]           # uncovered chain -> covered chain to absorb its weight

    @classmethod
    def load(cls, path: Path | None = None) -> StoreWeightsConfig | None:
        """Load store weights. Returns None if file doesn't exist."""
        if path is None:
            path = Path(__file__).parent.parent / "config" / "store_weights.json"
        if not path.exists():
            return None
        with open(path) as f:
            data = json.load(f)
        return cls(
            weights=data["weights"],
            proxy_chains=data.get("proxy_chains", {}),
        )

    def get_weight(self, county: str, chain: str) -> float | None:
        """Get the market share weight for a chain in a county.

        Returns None if county or chain not found.
        """
        county_weights = self.weights.get(county)
        if county_weights is None:
            return None
        return county_weights.get(chain)

    def effective_weights(self, county: str, present_chains: list[str]) -> dict[str, float]:
        """Return renormalized weights for chains that have price data.

        For each chain in the county that is NOT in present_chains, its weight
        is redistributed to its proxy (if configured) or dropped proportionally.
        Only weights for chains in present_chains are returned; they sum to 1.0.
        """
        county_weights = self.weights.get(county, {})
        if not county_weights:
            # No weights for this county — equal weighting
            return {c: 1.0 / len(present_chains) for c in present_chains}

        # Accumulate weights for each present chain, absorbing proxied chains.
        accumulated: dict[str, float] = {c: 0.0 for c in present_chains}
        for chain, w in county_weights.items():
            if chain in present_chains:
                accumulated[chain] += w
            else:
                proxy = self.proxy_chains.get(chain)
                if proxy and proxy in present_chains:
                    accumulated[proxy] += w
                # else: chain is uncovered and has no proxy → weight dropped

        total = sum(accumulated.values())
        if total == 0:
            return {c: 1.0 / len(present_chains) for c in present_chains}

        return {c: w / total for c, w in accumulated.items()}

    def coverage(self, county: str, present_chains: list[str]) -> float:
        """Fraction of market weight covered (directly or via proxy) by present_chains."""
        county_weights = self.weights.get(county, {})
        if not county_weights:
            return 1.0
        covered = sum(
            w for chain, w in county_weights.items()
            if chain in present_chains
            or self.proxy_chains.get(chain) in present_chains
        )
        return covered / sum(county_weights.values())


@dataclass
class HouseholdConfig:
    """Loaded household scaling configuration."""

    household_types: dict
    individual_shares: dict
    size_multipliers: dict

    @classmethod
    def load(cls, path: Path | None = None) -> HouseholdConfig:
        if path is None:
            path = Path(__file__).parent.parent / "config" / "household_scales.json"
        with open(path) as f:
            data = json.load(f)
        return cls(
            household_types=data["household_types"],
            individual_shares=data["individual_shares"],
            size_multipliers=data["household_size_multipliers"],
        )
