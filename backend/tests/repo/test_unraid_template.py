"""The Unraid catalog exposes one unified app and retains deprecated legacy paths."""

from __future__ import annotations

from xml.etree import ElementTree

import pytest

from app.modules.administration import setup_policy
from tests.paths import REPO_ROOT


def _template() -> ElementTree.Element:
    return ElementTree.parse(REPO_ROOT / "templates/printstash.xml").getroot()


def _configs(kind: str) -> list[ElementTree.Element]:
    return _template().findall(f"Config[@Type='{kind}']")


def _variables() -> dict[str | None, ElementTree.Element]:
    return {item.get("Target"): item for item in _configs("Variable")}


ADMIN_SETTINGS = [
    "VAULT_SETUP_ADMIN_USERNAME",
    "VAULT_SETUP_ADMIN_PASSWORD",
    "VAULT_SETUP_ADMIN_EMAIL",
]


class TestPrintStashTemplate:
    def test_selects_the_unified_image(self) -> None:
        template = _template()

        assert template.findtext("Name") == "PrintStash"
        assert (
            template.findtext("Repository") == "ghcr.io/xiao-villamor/printstash:latest"
        )
        assert template.findtext("Network") == "bridge"
        assert template.findtext("TemplateURL", "").endswith(
            "/templates/printstash.xml"
        )
        assert not template.findtext("PostArgs")

    def test_exposes_only_the_web_port(self) -> None:
        assert [(item.get("Target"), item.text) for item in _configs("Port")] == [
            ("3000", "3000")
        ]
        assert _template().findtext("WebUI") == "http://[IP]:[PORT:3000]/"

    def test_persists_the_data_parent(self) -> None:
        assert [
            (item.get("Target"), item.get("Default"), item.get("Mode"), item.text)
            for item in _configs("Path")
        ] == [
            (
                "/data",
                "/mnt/user/appdata/printstash",
                "rw",
                "/mnt/user/appdata/printstash",
            )
        ]

    def test_warns_that_a_second_data_mapping_stops_hard_links(self) -> None:
        (appdata,) = _configs("Path")
        description = appdata.get("Description", "")

        assert "one pool" in description
        assert "subfolder of /data" in description
        assert "hard-link" in description

    def test_enables_first_run_without_a_blank_secret(self) -> None:
        variables = {item.get("Target"): item for item in _configs("Variable")}

        assert variables["VAULT_SETUP_MODE"].text == "trusted_network"
        assert "VAULT_JWT_SECRET" not in variables

    def test_uses_unraid_file_identity(self) -> None:
        variables = {item.get("Target"): item for item in _configs("Variable")}

        assert variables["PUID"].text == "99"
        assert variables["PGID"].text == "100"

    @pytest.mark.parametrize("target", ADMIN_SETTINGS, ids=str)
    def test_shows_the_administrator_fields_up_front(self, target: str) -> None:
        assert _variables()[target].get("Display") == "always", (
            "hidden under Advanced, a field reads as missing (#248)"
        )

    @pytest.mark.parametrize("target", ADMIN_SETTINGS, ids=str)
    def test_leaves_the_administrator_optional(self, target: str) -> None:
        field = _variables()[target]

        assert (field.get("Required"), field.text) == ("false", None), (
            "blank fields fall back to browser registration on the LAN"
        )

    def test_warns_that_blank_fields_mean_local_network_registration(self) -> None:
        # Left blank, the first administrator can only register from the LAN;
        # someone arriving through Tailscale or a domain must know to fill it in.
        description = _variables()["VAULT_SETUP_ADMIN_USERNAME"].get("Description", "")

        assert "local network" in description, description

    def test_offers_both_first_run_modes_as_a_choice(self) -> None:
        # Unraid renders a pipe-separated Default as a dropdown.
        mode = _variables()["VAULT_SETUP_MODE"]

        assert mode.get("Default", "").split("|") == ["trusted_network", "environment"]

    def test_shows_the_first_run_mode_up_front(self) -> None:
        assert _variables()["VAULT_SETUP_MODE"].get("Display") == "always", (
            "the credentials only apply with environment, so the choice must be seen"
        )

    def test_ships_defaults_that_register_in_the_browser(self) -> None:
        # Resolved by the real policy: an untouched template must not boot
        # misconfigured.
        variables = _variables()

        policy = setup_policy.resolve(
            variables["VAULT_SETUP_MODE"].text or "",
            variables["VAULT_SETUP_ADMIN_USERNAME"].text or "",
            variables["VAULT_SETUP_ADMIN_PASSWORD"].text or "",
            variables["VAULT_SETUP_ADMIN_EMAIL"].text or "",
        )

        assert isinstance(policy, setup_policy.TrustedNetwork), policy

    def test_masks_the_administrator_password(self) -> None:
        assert _variables()["VAULT_SETUP_ADMIN_PASSWORD"].get("Mask") == "true"

    @pytest.mark.parametrize("target", ["PUID", "PGID"], ids=str)
    def test_shows_the_file_identity_up_front(self, target: str) -> None:
        assert _variables()[target].get("Display") == "always", (
            "hidden under Advanced, a field reads as missing (#248)"
        )

    def test_restarts_the_supervised_container(self) -> None:
        variables = {item.get("Target"): item for item in _configs("Variable")}

        assert variables["VAULT_RESTART_ENABLED"].text == "true"
        assert _template().findtext("ExtraParams") == "--restart=unless-stopped"


class TestPrintStashTemplateGpu:
    def test_offers_the_render_device_without_a_default(self) -> None:
        (device,) = _configs("Device")

        assert device.get("Target") == "/dev/dri"
        assert (device.get("Default"), device.get("Required")) == ("", "false"), (
            "a default device would stop the container on hosts without a GPU"
        )

    def test_keeps_the_nvidia_runtime_out_of_the_default_parameters(self) -> None:
        # --runtime=nvidia fails on a host without the Nvidia Driver plugin.
        assert "nvidia" not in _template().findtext("ExtraParams", "")
        assert "--runtime=nvidia" in _configs("Device")[0].get("Description", "")

    def test_defaults_the_nvidia_capabilities_to_all(self) -> None:
        assert _variables()["NVIDIA_DRIVER_CAPABILITIES"].text == "all"

    @pytest.mark.parametrize(
        ("target", "default"),
        [
            ("VAULT_COMPUTE_MODE", "auto"),
            ("VAULT_COMPUTE_RENDER_POLICY", "qualified"),
            ("VAULT_COMPUTE_MEMORY_MB", "1024"),
            ("VAULT_MESH_MEMORY_BUDGET_FRACTION", "0.5"),
            ("VAULT_EMBEDDING_MEMORY_BUDGET_FRACTION", "0.25"),
        ],
        ids=str,
    )
    def test_ships_the_documented_compute_defaults(
        self, target: str, default: str
    ) -> None:
        field = _variables()[target]

        assert (field.text, field.get("Display")) == (default, "advanced")

    def test_ships_memory_defaults_the_settings_accept(self) -> None:
        from app.core.config import Settings

        variables = _variables()
        settings = Settings(
            compute_mode=variables["VAULT_COMPUTE_MODE"].text,
            compute_render_policy=variables["VAULT_COMPUTE_RENDER_POLICY"].text,
            compute_memory_mb=int(variables["VAULT_COMPUTE_MEMORY_MB"].text or 0),
            mesh_memory_budget_fraction=float(
                variables["VAULT_MESH_MEMORY_BUDGET_FRACTION"].text or 0
            ),
            embedding_memory_budget_fraction=float(
                variables["VAULT_EMBEDDING_MEMORY_BUDGET_FRACTION"].text or 0
            ),
        )

        assert settings.compute_memory_mb == 1024


class TestCommunityApplicationsProfile:
    def test_community_applications_profile_describes_one_container(self) -> None:
        profile = ElementTree.parse(REPO_ROOT / "ca_profile.xml").getroot()
        description = profile.findtext("Profile", "")

        assert "one container" in description
        assert "two-container users" in description
        assert "Install **PrintStash-API first**" not in description


class TestTemplateCatalog:
    def test_only_one_current_catalog_template(self) -> None:
        templates = [
            ElementTree.parse(path).getroot()
            for path in sorted((REPO_ROOT / "templates").glob("printstash*.xml"))
        ]
        current = [item for item in templates if item.findtext("Deprecated") != "true"]

        assert len(current) == 1
        assert current[0].findtext("Name") == "PrintStash"

    @pytest.mark.parametrize(
        ("filename", "name", "image"),
        [
            ("printstash-api.xml", "PrintStash-API", "printstash-api"),
            ("printstash-frontend.xml", "PrintStash-Frontend", "printstash-frontend"),
        ],
        ids=["api", "frontend"],
    )
    def test_legacy_catalog_templates_are_deprecated(
        self, filename: str, name: str, image: str
    ) -> None:
        template = ElementTree.parse(REPO_ROOT / "templates" / filename).getroot()

        assert template.findtext("Deprecated") == "true"
        assert template.findtext("Name") == name
        assert template.findtext("Repository") == (
            f"ghcr.io/xiao-villamor/{image}:latest"
        )
        assert template.findtext("TemplateURL", "").endswith(f"/templates/{filename}")
