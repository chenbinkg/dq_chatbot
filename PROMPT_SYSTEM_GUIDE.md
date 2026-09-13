# Pre-Configured Prompts System for Collibra DQ Chatbot

## Overview

The chatbot UI now supports **pre-configured prompts** that allow users to quickly select common tasks and fill in required information. This reduces friction and ensures consistent prompt structure for the LLM agent.

## How It Works

### User Workflow

1. **Open Chat Tab** → User sees "Quick Prompts" section at the top
2. **Select Category** → Dropdown menu shows categories (Update Dataset, Create Dataset, JIRA Operations, etc.)
3. **Select Prompt** → Second dropdown shows specific prompts in that category
4. **Fill Form Fields** → Form dynamically displays required input fields with labels, placeholders, and field type hints
5. **Click "Use This Prompt"** → Template is rendered with user values and placed in the message field
6. **Review & Submit** → User can review the message and click Submit (or edit manually)

### Architecture

```
┌─────────────────────────────────────────────────┐
│  prompt_templates.json (Configuration)          │
│  - 15+ pre-defined prompts                       │
│  - Organized by category                         │
│  - Field definitions with validation             │
└─────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────┐
│  prompt_manager.py (Manager)                    │
│  - Load & parse templates                        │
│  - Validate field values                         │
│  - Render final prompts                          │
└─────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────┐
│  app.py (Gradio UI)                             │
│  - Category + Prompt dropdowns                   │
│  - Dynamic form fields (up to 8)                 │
│  - Submit button to render & populate msg field │
└─────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────┐
│  Agent (Existing Chat System)                    │
│  - Receives rendered prompt                      │
│  - Executes with LLM + tools                     │
└─────────────────────────────────────────────────┘
```

## Customizing Prompts

### Adding a New Prompt

1. Open `prompt_templates.json`
2. Add a new entry to the `prompts` array:

```json
{
  "id": "unique_id_for_prompt",
  "category": "Category Name",
  "title": "Display Title",
  "description": "Short description shown in dropdown",
  "template": "I want to... {field_name_1} ... and {field_name_2}",
  "fields": [
    {
      "name": "field_name_1",
      "label": "Label Shown in Form",
      "type": "text",
      "required": true,
      "placeholder": "e.g., example value"
    },
    {
      "name": "field_name_2",
      "label": "Another Field",
      "type": "textarea",
      "required": false,
      "placeholder": "Multi-line text input"
    }
  ]
}
```

### Field Types

| Type | Behavior | Use Cases |
|------|----------|-----------|
| `text` | Single-line input | Names, IDs, simple values |
| `textarea` | Multi-line input | Descriptions, SQL, long text |
| `email` | Email validation | Email addresses |
| `number` | Numeric input | Thresholds, counts |

### Template Syntax

- Use `{field_name}` in the template string
- Field names must match exactly (case-sensitive)
- All fields referenced in template must be defined in `fields` array

**Example Template:**
```json
"template": "I want to update the business unit for dataset '{dataset_name}' to '{new_bu}'. Current BU: '{current_bu}'."
```

### Modifying Existing Prompts

1. Locate the prompt by `id` in `prompt_templates.json`
2. Update the template or fields
3. Restart the app to reload the configuration

**Changes are live** - no code modification needed for prompt content updates.

## Current Prompt Categories & Prompts

### Update Dataset (6 prompts)
- Update Business Unit
- Update Email Alert
- Update Dataset Definitions
- Update LinkId
- Update Data Domain Tagging
- Update Sub-Domain Tagging

### Create Dataset (2 prompts)
- Create New S3 Dataset
- Create New Redshift Dataset

### Custom Rules (2 prompts)
- Create DQ Custom Rule
- Update DQ Custom Rule

### JIRA Operations (3 prompts)
- Query DQ JIRA Tickets
- Submit DQ JIRA Ticket (New DQ Setup)
- Submit DQ JIRA Ticket (Change Request)

### Query & Inspect (2 prompts)
- Inspect DQ Findings
- Inspect DQ Configurations

### Information (1 prompt)
- DQ Best Practices

## Advanced Usage

### Validating Field Values

The `PromptTemplate.render()` method validates:
- ✅ All required fields are provided
- ✅ Field values are non-empty strings
- ✅ Template substitution succeeds

Returns missing fields if validation fails.

### Example: Custom Validation

If you need domain-specific validation (e.g., dataset name format), modify `prompt_manager.py`:

```python
def render(self, values: dict) -> tuple[str, list[str]]:
    missing = []
    
    # Add custom validation
    for field in self.fields:
        name = field["name"]
        if name == "dataset_name" and values.get(name):
            # Check naming convention
            if not values[name].startswith("ds_"):
                missing.append(f"{name} must start with 'ds_'")
    
    # ... rest of validation
```

### Organizing Prompts by User Role

You can extend the system to show role-specific prompts by modifying the `get_prompt_categories()` function:

```python
def get_prompt_categories(user_role=None):
    """Return categories filtered by user role."""
    all_categories = prompt_manager.get_categories()
    if user_role == "admin":
        return all_categories
    elif user_role == "analyst":
        # Hide sensitive categories
        return [c for c in all_categories if c not in ["JIRA Operations"]]
    return all_categories
```

### Storing Custom Prompts in Database

For user-created prompts, extend the system:

1. Create a `user_prompts` table in your chat_store database
2. Modify `PromptTemplateManager` to load from both `prompt_templates.json` + database
3. Add UI for saving/editing user prompts

## Troubleshooting

### Prompt Not Appearing

- Check that `prompt_templates.json` exists in `collibra_dq_app/` directory
- Verify JSON syntax (use online JSON validator)
- Restart the app to reload configuration
- Check logs for load errors: `Loaded X prompt templates`

### Form Fields Not Showing

- Ensure category and prompt are selected (both dropdowns)
- Verify field names in `fields` array match template variables
- Check that `required` field values are boolean (`true`/`false`)

### Template Not Rendering

- Confirm all required fields have values entered
- Check that field names in template (e.g., `{dataset_name}`) match field definitions
- Verify no typos in field names (case-sensitive)

## Performance Notes

- Prompts are loaded into memory at app startup
- No external API calls for template management
- Adding many prompts (100+) has negligible impact

## Future Enhancements

Potential improvements:

1. **Prompt Versioning** - Track prompt changes over time
2. **Usage Analytics** - Track which prompts are most used
3. **AI-Suggested Prompts** - Recommend prompts based on conversation context
4. **Conditional Fields** - Show/hide fields based on previous selections
5. **Prompt Chaining** - Suggest follow-up prompts after completion
6. **Multi-language Support** - Translate prompts for different regions
7. **Accessibility** - Add screen reader support for forms

## Files Modified

| File | Changes |
|------|---------|
| `app.py` | Added prompt manager initialization, form UI, callbacks |
| `prompt_manager.py` | **New** - Template loading & rendering |
| `prompt_templates.json` | **New** - Prompt definitions |

## Testing the System

```bash
# Test prompt loading
python -c "from prompt_manager import PromptTemplateManager; pm = PromptTemplateManager(); print(f'Loaded {len(pm.templates)} prompts')"

# Run app
python app.py
```

Visit `http://localhost:7860` and navigate to Chat tab to test.

