"""Tests for shared.routing.offers -- model_configs offer loading + fleet placement hook."""

from unittest.mock import AsyncMock, Mock

from shared.routing.capability import ModelOffer
from shared.routing.offers import load_offers_from_model_configs


def _fake_db_with_rows(rows):
    """A minimal fake penguin-dal db exposing model_configs.enabled == True -> rows."""
    db = Mock()
    db.model_configs = Mock()
    db.model_configs.enabled = True
    query_result = Mock()
    query_result.select = Mock(return_value=rows)
    db.return_value = query_result
    return db


def _row(model_name, providers=None, cost=None, context_length=8192, capabilities=None):
    """A model_configs row shape, as load_offers_from_model_configs expects."""
    row = Mock()
    row.model_name = model_name
    row.preferred_providers = providers or ["openai"]
    row.cost_per_token = cost or {"openai": 0.00003}
    row.context_length = context_length
    row.capabilities = capabilities or []
    return row


class TestLoadOffersBasics:
    """Location/cost inference from model_configs, unchanged by fleet placement."""

    async def test_infers_local_location_from_preferred_providers(self):
        """An ollama/llamacpp preferred provider marks the offer local."""
        db = _fake_db_with_rows([_row("llama3.1:8b", providers=["ollama"])])
        offers = await load_offers_from_model_configs(db)
        assert offers[0].location == "local"

    async def test_infers_commercial_location(self):
        """A non-local preferred provider marks the offer commercial."""
        db = _fake_db_with_rows([_row("gpt-4o", providers=["openai"])])
        offers = await load_offers_from_model_configs(db)
        assert offers[0].location == "commercial"

    async def test_no_placement_leaves_offers_untouched(self):
        """Omitting placement/backends is byte-identical to before fleet placement existed."""
        db = _fake_db_with_rows([_row("gpt-4o")])
        offers = await load_offers_from_model_configs(db)
        assert offers[0].available is True


class TestLoadOffersFleetAnnotation:
    """Optional placement/backends args (spec §10.4) -- fleet-live-state annotation hook."""

    async def test_placement_and_backends_annotate_local_offers(self):
        """When both are given, offers are replaced by PlacementEngine.annotate_offers' result."""
        db = _fake_db_with_rows([_row("local-model", providers=["ollama"])])
        placement = Mock()
        annotated = [ModelOffer(model_name="local-model", location="local", available=False)]
        placement.annotate_offers = AsyncMock(return_value=annotated)
        backends = ["fake-backend"]

        offers = await load_offers_from_model_configs(db, placement=placement, backends=backends)

        placement.annotate_offers.assert_awaited_once()
        called_offers_arg, called_backends_arg = placement.annotate_offers.call_args.args
        assert called_offers_arg[0].model_name == "local-model"
        assert called_backends_arg == backends
        assert offers == annotated

    async def test_placement_without_backends_defaults_to_empty_list(self):
        """A placement engine with no backends list still gets called, with `[]`."""
        db = _fake_db_with_rows([_row("local-model", providers=["ollama"])])
        placement = Mock()
        placement.annotate_offers = AsyncMock(side_effect=lambda offers, backends: offers)

        await load_offers_from_model_configs(db, placement=placement, backends=None)

        placement.annotate_offers.assert_awaited_once()
        _, called_backends_arg = placement.annotate_offers.call_args.args
        assert called_backends_arg == []
