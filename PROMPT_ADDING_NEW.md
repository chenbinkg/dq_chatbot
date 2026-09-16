# Quick Reference: Adding New Prompts

## Step-by-Step Example

### Scenario: Add "Export Dataset to CSV" prompt

#### Step 1: Edit `prompt_templates.json`

Locate the `prompts` array and add your new prompt entry:

```json
{
  "id": "export_dataset_csv",
  "category": "Dataset Operations",
  "title": "Export Dataset to CSV",
  "description": "Export a dataset to S3 as CSV file",
  "template": "I want to export dataset '{dataset_name}' to CSV format. S3 destination: '{s3_destination}'. Include headers: {include_headers}. Compression: '{compression}'.",
  "fields": [
    {
      "name": "dataset_name",
      "label": "Dataset Name",
      "type": "text",
      "required": true,
      "placeholder": "e.g., ds_customer_data"
    },
    {
      "name": "s3_destination",
      "label": "S3 Destination Path",
      "type": "text",
      "required": true,
      "placeholder": "s3://my-bucket/exports/customers/"
    },
    {
      "name": "include_headers",
      "label": "Include Headers",
      "type": "text",
      "required": true,
      "placeholder": "yes or no"
    },
    {
      "name": "compression",
      "label": "Compression Type",
      "type": "text",
      "required": false,
      "placeholder": "e.g., none, gzip, snappy"
    }
  ]
}
```

#### Step 2: Test

1. Restart the app
2. Go to Chat tab
3. Select "Dataset Operations" category
4. Select "Export Dataset to CSV" prompt
5. Fill in the form:
   - Dataset Name: `ds_customer_data`
   - S3 Destination: `s3://my-bucket/exports/`
   - Include Headers: `yes`
   - Compression: `gzip`
6. Click "Use This Prompt"
7. Verify rendered message in text field

#### Expected Output

```
I want to export dataset 'ds_customer_data' to CSV format. 
S3 destination: 's3://my-bucket/exports/'. 
Include headers: yes. 
Compression: 'gzip'.
```

## Common Patterns

### Pattern 1: Simple Two-Field Prompt

```json
{
  "id": "get_dataset_info",
  "category": "Query & Inspect",
  "title": "Get Dataset Information",
  "description": "Retrieve metadata for a dataset",
  "template": "Can you provide information about dataset '{dataset_name}'? Include stats for the last '{days}' days.",
  "fields": [
    {
      "name": "dataset_name",
      "label": "Dataset Name",
      "type": "text",
      "required": true,
      "placeholder": "e.g., ds_transactions"
    },
    {
      "name": "days",
      "label": "Days Back",
      "type": "number",
      "required": true,
      "placeholder": "e.g., 30"
    }
  ]
}
```

### Pattern 2: Complex Multi-field Setup

```json
{
  "id": "setup_monitoring",
  "category": "Create Dataset",
  "title": "Setup Dataset Monitoring",
  "description": "Configure comprehensive monitoring for new dataset",
  "template": "Setup comprehensive DQ monitoring for new dataset:\n- Name: '{dataset_name}'\n- Source: {source_type}\n- Business Unit: '{business_unit}'\n- Data Domain: '{data_domain}'\n- Sub-domain: '{sub_domain}'\n- Alert Recipients: '{alert_recipients}'\n- Rule Template: '{rule_template}'",
  "fields": [
    {
      "name": "dataset_name",
      "label": "Dataset Name",
      "type": "text",
      "required": true,
      "placeholder": "ds_new_dataset"
    },
    {
      "name": "source_type",
      "label": "Source Type (S3/Redshift/API)",
      "type": "text",
      "required": true,
      "placeholder": "S3 or Redshift"
    },
    {
      "name": "business_unit",
      "label": "Business Unit",
      "type": "text",
      "required": true,
      "placeholder": "Sales, Marketing, Finance"
    },
    {
      "name": "data_domain",
      "label": "Data Domain",
      "type": "text",
      "required": true,
      "placeholder": "Customer, Product, Financial"
    },
    {
      "name": "sub_domain",
      "label": "Sub-Domain",
      "type": "text",
      "required": true,
      "placeholder": "Demographics, Transactions, Account"
    },
    {
      "name": "alert_recipients",
      "label": "Alert Email Recipients",
      "type": "text",
      "required": true,
      "placeholder": "user1@company.com, user2@company.com"
    },
    {
      "name": "rule_template",
      "label": "Rule Template Description",
      "type": "textarea",
      "required": false,
      "placeholder": "Describe default rules to create (e.g., nullness checks, uniqueness)"
    }
  ]
}
```

### Pattern 3: Investigation/Troubleshooting

```json
{
  "id": "investigate_failure",
  "category": "Troubleshooting",
  "title": "Investigate Rule Failure",
  "description": "Debug why a DQ rule is failing",
  "template": "I want to investigate why rule '{rule_name}' is failing for dataset '{dataset_name}':\n\nFirst Failure Date: {failure_date}\nFailure Pattern: '{failure_pattern}'\nLast Known Good Run: {last_good_run}\nExpected Behavior: '{expected_behavior}'",
  "fields": [
    {
      "name": "rule_name",
      "label": "Rule Name",
      "type": "text",
      "required": true,
      "placeholder": "e.g., unique_customer_id"
    },
    {
      "name": "dataset_name",
      "label": "Dataset Name",
      "type": "text",
      "required": true,
      "placeholder": "e.g., ds_customers"
    },
    {
      "name": "failure_date",
      "label": "When Did It Start Failing",
      "type": "text",
      "required": true,
      "placeholder": "YYYY-MM-DD or 'Last 3 days'"
    },
    {
      "name": "failure_pattern",
      "label": "Failure Pattern Description",
      "type": "textarea",
      "required": true,
      "placeholder": "Describe what's failing (e.g., 'Nulls in customer_id column', 'Duplicates increased 500%')"
    },
    {
      "name": "last_good_run",
      "label": "Last Successful Run",
      "type": "text",
      "required": false,
      "placeholder": "YYYY-MM-DD or time ago"
    },
    {
      "name": "expected_behavior",
      "label": "Expected Behavior",
      "type": "textarea",
      "required": true,
      "placeholder": "What should this rule do?"
    }
  ]
}
```

## Tips & Best Practices

### ✅ DO

- Use descriptive field labels (`Customer Name` not `cname`)
- Provide helpful placeholders with real examples
- Mark truly required fields with `required: true`
- Use `textarea` for multi-line input (SQL, descriptions)
- Group related fields in one prompt
- Test after adding/modifying prompts

### ❌ DON'T

- Don't use field names with spaces or special characters
- Don't make optional fields required if not needed
- Don't add more than 8 fields per prompt (form gets unwieldy)
- Don't use confusing template variable names
- Don't duplicate prompts with slightly different names

## Validation Rules

When you fill the form and click "Use This Prompt":

1. ✅ All `required: true` fields must have values
2. ✅ Template variables `{field_name}` must exist in fields
3. ✅ No extra braces or special characters in template
4. ❌ Missing required field → Error message shown
5. ❌ Invalid template → Generic error (check JSON)

## Debugging

### Prompt not appearing in dropdown?

```bash
# Check if JSON is valid
python3 -m json.tool collibra_dq_app/prompt_templates.json
```

### Want to see loaded prompts?

```python
from prompt_manager import PromptTemplateManager

pm = PromptTemplateManager()
for category in pm.get_categories():
    print(f"\n{category}:")
    for template in pm.get_templates_by_category(category):
        print(f"  - {template.title}")
```

### Render a template manually?

```python
from prompt_manager import PromptTemplateManager

pm = PromptTemplateManager()
template = pm.get_template("update_bu")

values = {
    "dataset_name": "ds_customers",
    "current_bu": "Sales",
    "new_bu": "Marketing"
}

prompt, missing = template.render(values)
if missing:
    print(f"Missing: {missing}")
else:
    print(prompt)
```

## JSON Syntax Quick Reference

```json
// String values - use quotes
"field_name": "dataset_name"

// Boolean values - lowercase, no quotes  
"required": true
"required": false

// Array of objects
"fields": [
  { "name": "field1" },
  { "name": "field2" }
]

// Placeholder text in template - use curly braces
"template": "... '{field_name}' ..."

// Template can span multiple lines with \n
"template": "Line 1\nLine 2"
```

