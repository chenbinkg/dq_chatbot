"""
Prompt template manager for pre-configured chatbot prompts.

Loads prompt templates from prompt_templates.json and provides
utilities to construct final prompts with user-provided field values.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PromptTemplate:
    """Represents a single prompt template with field definitions."""

    def __init__(self, template_dict: dict):
        self.id = template_dict.get("id")
        self.category = template_dict.get("category")
        self.title = template_dict.get("title")
        self.description = template_dict.get("description")
        self.template = template_dict.get("template")
        self.fields = template_dict.get("fields", [])

    def get_required_fields(self):
        """Return list of required field names."""
        return [f["name"] for f in self.fields if f.get("required", False)]

    def get_all_fields(self):
        """Return all field definitions."""
        return self.fields

    def render(self, values: dict) -> tuple[str, list[str]]:
        """
        Render the template with provided values.

        Args:
            values: Dictionary mapping field names to their values

        Returns:
            Tuple of (rendered_prompt, list_of_missing_required_fields)
        """
        missing = []
        render_dict = dict(values)

        # Check for missing required fields
        for field in self.fields:
            name = field["name"]
            if field.get("required", False) and not render_dict.get(name):
                missing.append(name)

        if missing:
            return "", missing

        # Blank optional fields would otherwise raise KeyError during format().
        for field in self.fields:
            render_dict.setdefault(field["name"], "")

        try:
            prompt = self.template.format(**render_dict)
            return prompt, []
        except KeyError as e:
            logger.error("Missing field in template: %s", e)
            return "", [str(e).strip("'")]

    def to_dict(self):
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "template": self.template,
            "fields": self.fields,
        }


class PromptTemplateManager:
    """Manages loading and accessing prompt templates."""

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the prompt manager.

        Args:
            config_path: Path to prompt_templates.json. If None, looks in the
                         same directory as this file.
        """
        if config_path is None:
            # Look for prompt_templates.json in the same directory as this file
            current_dir = Path(__file__).parent
            config_path = current_dir / "prompt_templates.json"
        else:
            config_path = Path(config_path)

        self.config_path = config_path
        self.templates: dict[str, PromptTemplate] = {}
        self.categories: dict[str, list[PromptTemplate]] = {}
        self._load_templates()

    def _load_templates(self):
        """Load templates from JSON configuration file."""
        if not self.config_path.exists():
            logger.warning("Prompt templates file not found: %s", self.config_path)
            return

        try:
            with open(self.config_path, "r") as f:
                config = json.load(f)

            for template_dict in config.get("prompts", []):
                template = PromptTemplate(template_dict)
                self.templates[template.id] = template

                # Organize by category
                if template.category not in self.categories:
                    self.categories[template.category] = []
                self.categories[template.category].append(template)

            logger.info("Loaded %d prompt templates from %s", len(self.templates), self.config_path)
        except Exception as e:
            logger.error("Failed to load prompt templates: %s", e)

    def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        """Get a template by ID."""
        return self.templates.get(template_id)

    def get_templates_by_category(self, category: str) -> list[PromptTemplate]:
        """Get all templates in a category."""
        return self.categories.get(category, [])

    def get_categories(self) -> list[str]:
        """Get list of all categories (sorted)."""
        return sorted(self.categories.keys())

    def get_all_templates(self) -> list[PromptTemplate]:
        """Get all templates."""
        return list(self.templates.values())

    def construct_prompt(self, template_id: str, field_values: dict) -> tuple[str, list[str]]:
        """
        Construct a final prompt using a template and field values.

        Args:
            template_id: ID of the template to use
            field_values: Dictionary of field name -> value mappings

        Returns:
            Tuple of (final_prompt, list_of_missing_required_fields)
            If there are missing required fields, final_prompt will be empty.
        """
        template = self.get_template(template_id)
        if not template:
            return "", [f"Template '{template_id}' not found"]

        return template.render(field_values)
