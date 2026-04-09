"""CIS WorkBench scraper strategy for current HTML structure (October 2025).

This strategy extracts ALL fields including:
- HTML content from <div id="..."> elements
- Structured data from custom <wb-...> elements
- Parsed compliance mappings (MITRE, NIST, CIS Controls)
"""

from typing import Any

from bs4 import BeautifulSoup

from cis_bench.utils.parsers import WorkbenchParser

from .base import ScraperStrategy


class WorkbenchV1Strategy(ScraperStrategy):
    """Scraper for current CIS WorkBench HTML (as of October 2025).

    Extracts from both legacy and newer WorkBench layouts:
    1. <div id="*-recommendation-data"> elements
    2. <wb-recommendation-data attribute="..."> elements
    3. Other <wb-recommendation-*> custom elements with structured data
    """

    @property
    def version(self) -> str:
        return "v1_2025_10"

    @property
    def selectors(self) -> dict[str, dict[str, str]]:
        """Element ID selectors for HTML content fields."""
        return {
            "assessment_html": {"id": "automated_scoring-recommendation-data"},
            "description": {"id": "description-recommendation-data"},
            "rationale": {"id": "rationale_statement-recommendation-data"},
            "impact": {"id": "impact_statement-recommendation-data"},
            "audit": {"id": "audit_procedure-recommendation-data"},
            "remediation": {"id": "remediation_procedure-recommendation-data"},
            "default_value": {"id": "default_value-recommendation-data"},
            "artifact_equation": {"id": "artifact_equation-recommendation-data"},
            "mitre_mapping_html": {"id": "mitre_mappings-recommendation-data"},
            "references": {"id": "references-recommendation-data"},
            "additional_info": {"id": "notes-recommendation-data"},
        }

    def _get_component_text(self, soup: BeautifulSoup, attribute: str) -> str | None:
        """Get text from newer wb-recommendation-data components."""
        elem = soup.find("wb-recommendation-data", attrs={"attribute": attribute})
        if not elem:
            return None
        return elem.get("text")

    def extract_recommendation(self, html: str) -> dict[str, Any]:
        """Extract ALL recommendation fields from HTML."""
        soup = BeautifulSoup(html, "html.parser")
        data = {}

        component_attribute_map = {
            "assessment_html": "automated_scoring",
            "description": "description",
            "rationale": "rationale_statement",
            "impact": "impact_statement",
            "audit": "audit_procedure",
            "remediation": "remediation_procedure",
            "default_value": "default_value",
            "artifact_equation": "artifact_equation",
            "references": "references",
            "additional_info": "notes",
        }

        # ============ Step 1: Extract from legacy divs with fallback to wb-recommendation-data ============
        for field, selector in self.selectors.items():
            elem = None
            if "id" in selector:
                elem = soup.find(id=selector["id"])

            if elem is not None:
                data[field] = elem.decode_contents().strip()
            else:
                attribute = component_attribute_map.get(field)
                data[field] = self._get_component_text(soup, attribute) if attribute else None

        # ============ Step 2: Extract from custom <wb-*> elements ============
        profiles_elem = soup.find("wb-recommendation-profiles")
        if profiles_elem and profiles_elem.get("profiles"):
            data["profiles"] = WorkbenchParser.parse_profiles_json(profiles_elem.get("profiles"))
        else:
            data["profiles"] = WorkbenchParser.parse_profile_text(
                self._get_component_text(soup, "profiles")
            )

        controls_elem = soup.find("wb-recommendation-feature-controls")
        controls_json = None
        if controls_elem:
            controls_json = controls_elem.get("json-controls")
            if not controls_json:
                parsed_controls = WorkbenchParser.parse_json_parse_attribute(
                    controls_elem.get(":controls")
                )
                if parsed_controls is not None:
                    import json

                    controls_json = json.dumps(parsed_controls)
        data["cis_controls"] = (
            WorkbenchParser.parse_cis_controls_json(controls_json) if controls_json else []
        )

        artifacts_elem = soup.find("wb-recommendation-artifacts")
        if artifacts_elem and artifacts_elem.get("artifacts-json"):
            data["artifacts"] = WorkbenchParser.parse_artifacts_json(
                artifacts_elem.get("artifacts-json")
            )
        else:
            data["artifacts"] = []

        mitre_elem = soup.find("wb-recommendation-mitre-mappings")
        if data.get("mitre_mapping_html"):
            data["mitre_mapping"] = WorkbenchParser.parse_mitre_table(data["mitre_mapping_html"])
        elif mitre_elem and mitre_elem.get(":mappings"):
            data["mitre_mapping"] = WorkbenchParser.parse_mitre_json(
                WorkbenchParser.parse_json_parse_attribute(mitre_elem.get(":mappings"))
            )
        else:
            data["mitre_mapping"] = None

        data["nist_controls"] = (
            WorkbenchParser.parse_nist_controls(data["references"]) if data.get("references") else []
        )

        if data.get("assessment_html"):
            data["assessment_status"] = WorkbenchParser.extract_assessment_status(
                data["assessment_html"]
            )
        else:
            data["assessment_status"] = "Unknown"

        data["parent"] = WorkbenchParser.parse_parent_link(html)

        data.pop("assessment_html", None)
        data.pop("mitre_mapping_html", None)

        return data

    def is_compatible(self, html: str) -> bool:
        """Check if this strategy works with given HTML."""
        soup = BeautifulSoup(html, "html.parser")

        found_divs = 0
        for field in ["description", "rationale", "audit"]:
            selector = self.selectors.get(field)
            if selector and "id" in selector and soup.find(id=selector["id"]):
                found_divs += 1

        has_legacy_wb_elements = bool(soup.find("wb-recommendation-profiles"))
        has_new_components = bool(soup.find("wb-recommendation-data", attrs={"attribute": "description"}))

        return found_divs >= 2 or has_legacy_wb_elements or has_new_components
