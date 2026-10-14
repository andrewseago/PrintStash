"""The app-store manifests under ``catalogues/`` describe the same installation.

Each store gets its own file, and nothing in the product imports them, so a manifest
that drifts from the image it runs fails only on a user's server: a store listing
pinned to an old version, a missing ``/data`` mount that starts every restart empty,
an empty ``VAULT_JWT_SECRET`` read as a deliberate choice, or an administrator field
that never reaches ``VAULT_SETUP_ADMIN_*``. These tests make each of those visible in
the pull request that causes it. The version pin is what ties a release to its
listings: the release commit bumps the app version, and this file fails until every
manifest follows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import Settings, settings
from app.modules.administration import setup_policy
from app.schemas.setup import SetupRequest
from tests.paths import REPO_ROOT

IMAGE = "ghcr.io/xiao-villamor/printstash"
CATALOGUES = REPO_ROOT / "catalogues"
RUNTIPI = CATALOGUES / "runtipi" / "printstash"
UMBREL = CATALOGUES / "umbrel" / "printstash"
CASAOS = CATALOGUES / "casaos" / "PrintStash"

# The service that runs PrintStash in each store's compose file.
MAIN_SERVICES = {
    "casaos": (CASAOS / "docker-compose.yml", "printstash"),
    "runtipi": (RUNTIPI / "docker-compose.yml", "printstash"),
    "umbrel": (UMBREL / "docker-compose.yml", "web"),
}
STORES = sorted(MAIN_SERVICES)
# Stores whose install always supplies VAULT_SETUP_ADMIN_* (required form fields on
# Runtipi, Umbrel's per-app credentials), and those whose fields may be left blank.
ALWAYS_PROVISIONED = ["runtipi", "umbrel"]
OPTIONALLY_PROVISIONED = ["casaos"]
ADMIN_SETTINGS = ["VAULT_SETUP_ADMIN_USERNAME", "VAULT_SETUP_ADMIN_PASSWORD"]
# The limits the browser wizard and VAULT_SETUP_ADMIN_* validation enforce.
WIZARD_LIMITS = SetupRequest.model_json_schema()["properties"]


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _service(store: str) -> dict[str, Any]:
    path, name = MAIN_SERVICES[store]
    return _load(path)["services"][name]


def _environment(store: str) -> dict[str, str]:
    """Environment as a mapping, whichever of Compose's two forms the store uses."""
    raw = _service(store)["environment"]
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    return dict(str(entry).split("=", 1) for entry in raw)


def _runtipi_fields() -> dict[str, dict[str, Any]]:
    config = json.loads((RUNTIPI / "config.json").read_text())
    return {item["env_variable"]: item for item in config["form_fields"]}


class TestEveryCatalogue:
    def test_covers_every_catalogue_directory(self) -> None:
        # A new store directory is guarded the day it is added, or this fails.
        stores = {path.name for path in CATALOGUES.iterdir() if path.is_dir()}

        assert stores == set(MAIN_SERVICES), "add the new store to MAIN_SERVICES"

    def test_classifies_every_store_by_how_it_provisions_the_owner(self) -> None:
        # Each store's setup mode follows from this, so none may be left out.
        assert sorted(ALWAYS_PROVISIONED + OPTIONALLY_PROVISIONED) == STORES

    @pytest.mark.parametrize("store", STORES, ids=str)
    def test_runs_the_unified_image_at_the_app_version(self, store: str) -> None:
        image = _service(store)["image"]

        assert image == f"{IMAGE}:{settings.app_version}", (
            "bump every catalogue manifest in the release commit"
        )

    @pytest.mark.parametrize("store", STORES, ids=str)
    def test_persists_the_data_root(self, store: str) -> None:
        targets = [
            volume["target"] if isinstance(volume, dict) else volume.split(":")[1]
            for volume in _service(store)["volumes"]
        ]

        assert "/data" in targets, "without /data every restart starts empty"

    @pytest.mark.parametrize("store", STORES, ids=str)
    def test_ships_compute_budgets_the_settings_accept(
        self, store: str
    ) -> None:
        env = _environment(store)

        resolved = Settings(
            compute_mode=env["VAULT_COMPUTE_MODE"],
            compute_memory_mb=int(env["VAULT_COMPUTE_MEMORY_MB"]),
            mesh_memory_budget_fraction=float(env["VAULT_MESH_MEMORY_BUDGET_FRACTION"]),
            embedding_memory_budget_fraction=float(
                env["VAULT_EMBEDDING_MEMORY_BUDGET_FRACTION"]
            ),
        )

        assert resolved.compute_mode == "auto"

    def test_describes_the_casaos_budgets_in_the_install_dialog(self) -> None:
        described = {
            item["container"]
            for item in _load(MAIN_SERVICES["casaos"][0])["services"]["printstash"][
                "x-casaos"
            ]["envs"]
        }

        assert {
            "VAULT_COMPUTE_MEMORY_MB",
            "VAULT_MESH_MEMORY_BUDGET_FRACTION",
            "VAULT_EMBEDDING_MEMORY_BUDGET_FRACTION",
        } <= described

    @pytest.mark.parametrize("store", ALWAYS_PROVISIONED, ids=str)
    def test_creates_the_owner_from_the_environment_where_it_is_always_supplied(
        self, store: str
    ) -> None:
        # The owner exists before the first request and the browser has no door,
        # so nothing through the store's proxy can claim the installation.
        assert _environment(store)["VAULT_SETUP_MODE"] == "environment"

    @pytest.mark.parametrize("store", OPTIONALLY_PROVISIONED, ids=str)
    def test_ships_defaults_that_register_in_the_browser(self, store: str) -> None:
        # Resolved by the real policy: an untouched install dialog must not boot
        # misconfigured.
        environment = _environment(store)

        policy = setup_policy.resolve(
            environment["VAULT_SETUP_MODE"],
            environment["VAULT_SETUP_ADMIN_USERNAME"],
            environment["VAULT_SETUP_ADMIN_PASSWORD"],
            environment["VAULT_SETUP_ADMIN_EMAIL"],
        )

        assert isinstance(policy, setup_policy.TrustedNetwork), policy

    @pytest.mark.parametrize("store", STORES, ids=str)
    def test_never_passes_a_jwt_secret(self, store: str) -> None:
        assert "VAULT_JWT_SECRET" not in _environment(store), (
            "an empty value is read as a deliberate choice and skips the generated one"
        )

    @pytest.mark.parametrize("store", STORES, ids=str)
    @pytest.mark.parametrize("variable", ADMIN_SETTINGS, ids=str)
    def test_wires_the_environment_administrator(
        self, store: str, variable: str
    ) -> None:
        assert variable in _environment(store)

    @pytest.mark.parametrize("store", STORES, ids=str)
    def test_offers_the_settings_restart(self, store: str) -> None:
        assert _environment(store)["VAULT_RESTART_ENABLED"] == "true"

    @pytest.mark.parametrize("store", STORES, ids=str)
    def test_relaunches_after_a_graceful_exit(self, store: str) -> None:
        assert _service(store)["restart"] == "unless-stopped", (
            "a Settings restart is a graceful SIGTERM that can exit 0; "
            "on-failure would leave the app stopped"
        )

    @pytest.mark.parametrize("store", STORES, ids=str)
    def test_gives_both_processes_time_to_stop(self, store: str) -> None:
        assert _service(store)["stop_grace_period"] == "60s"


class TestRuntipi:
    def test_routes_to_the_web_port(self) -> None:
        assert _service("runtipi")["x-runtipi"] == {
            "is_main": True,
            "internal_port": 3000,
        }

    def test_declares_the_app_version(self) -> None:
        config = json.loads((RUNTIPI / "config.json").read_text())

        assert config["version"] == settings.app_version

    def test_requires_a_runtipi_that_reads_dynamic_compose(self) -> None:
        # x-runtipi schema_version 2 is not understood by older Runtipi releases.
        config = json.loads((RUNTIPI / "config.json").read_text())

        assert config["min_tipi_version"] == "4.5.0"

    @pytest.mark.parametrize(
        ("field", "variable"),
        [
            pytest.param(
                "PRINTSTASH_ADMIN_USERNAME", "VAULT_SETUP_ADMIN_USERNAME", id="username"
            ),
            pytest.param(
                "PRINTSTASH_ADMIN_PASSWORD", "VAULT_SETUP_ADMIN_PASSWORD", id="password"
            ),
            pytest.param(
                "PRINTSTASH_ADMIN_EMAIL", "VAULT_SETUP_ADMIN_EMAIL", id="email"
            ),
        ],
    )
    def test_passes_each_form_field_to_its_setting(
        self, field: str, variable: str
    ) -> None:
        assert _environment("runtipi")[variable] == f"${{{field}}}"

    @pytest.mark.parametrize(
        "field", ["PRINTSTASH_ADMIN_USERNAME", "PRINTSTASH_ADMIN_PASSWORD"], ids=str
    )
    def test_requires_the_administrator_credentials(self, field: str) -> None:
        # Required fields mean the owner exists before the app serves a request,
        # so no browser ever has to claim it through Runtipi's proxy.
        assert _runtipi_fields()[field]["required"] is True

    @pytest.mark.parametrize(
        ("field", "setting"),
        [
            pytest.param("PRINTSTASH_ADMIN_USERNAME", "username", id="username"),
            pytest.param("PRINTSTASH_ADMIN_PASSWORD", "password", id="password"),
        ],
    )
    def test_enforces_the_wizard_limits(self, field: str, setting: str) -> None:
        # A form that accepts less than startup validation leaves no administrator.
        form = _runtipi_fields()[field]

        assert (form["min"], form["max"]) == (
            WIZARD_LIMITS[setting]["minLength"],
            WIZARD_LIMITS[setting]["maxLength"],
        )


class TestUmbrel:
    def test_routes_the_app_proxy_to_the_web_service(self) -> None:
        proxy = _load(UMBREL / "docker-compose.yml")["services"]["app_proxy"]

        assert (proxy["environment"]["APP_HOST"], proxy["environment"]["APP_PORT"]) == (
            "printstash_web_1",
            3000,
        ), "Umbrel names containers <app id>_<service>_1"

    def test_declares_the_app_version(self) -> None:
        manifest = _load(UMBREL / "umbrel-app.yml")

        assert manifest["version"] == settings.app_version

    def test_provisions_the_username_umbrel_shows(self) -> None:
        manifest = _load(UMBREL / "umbrel-app.yml")

        assert (
            _environment("umbrel")["VAULT_SETUP_ADMIN_USERNAME"]
            == manifest["defaultUsername"]
        )

    def test_asks_umbrel_for_a_password_to_show(self) -> None:
        assert _load(UMBREL / "umbrel-app.yml")["deterministicPassword"] is True

    def test_provisions_the_password_umbrel_shows(self) -> None:
        assert _environment("umbrel")["VAULT_SETUP_ADMIN_PASSWORD"] == "${APP_PASSWORD}"


class TestCasaOS:
    def test_routes_the_published_port_to_the_web_port(self) -> None:
        compose = _load(CASAOS / "docker-compose.yml")
        port = _service("casaos")["ports"][0]

        assert (port["target"], port["published"], compose["x-casaos"]["port_map"]) == (
            3000,
            compose["x-casaos"]["port_map"],
            "3378",
        ), "the dashboard opens port_map, so it must be the published port"

    def test_declares_the_app_version(self) -> None:
        compose = _load(CASAOS / "docker-compose.yml")

        assert compose["x-casaos"]["version"] == settings.app_version

    @pytest.mark.parametrize("variable", ADMIN_SETTINGS, ids=str)
    def test_leaves_the_administrator_blank_for_browser_registration(
        self, variable: str
    ) -> None:
        assert _environment("casaos")[variable] == "", (
            "blank is unset, so an untouched install dialog registers in the browser"
        )

    @pytest.mark.parametrize("variable", ["PUID", "PGID"], ids=str)
    def test_uses_the_host_file_identity(self, variable: str) -> None:
        assert _environment("casaos")[variable] == f"${variable}"
