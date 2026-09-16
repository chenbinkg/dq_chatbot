# Pre-Configured Prompts UI Walkthrough

## Visual Flow

### Screen 1: Chat Tab (Initial State)

```
┌─────────────────────────────────────────────────────────────┐
│                  Collibra DQ Chatbot                        │
├──────────┬──────────────────────────────────────────────────┤
│ Login    │ Chat                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ ### Quick Prompts                                           │
│ Select a pre-configured prompt to get started, or type a   │
│ custom message below.                                       │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Category: [▼ Select Category ▼]                          │ │
│ │ Prompt:   [▼ Select Prompt ▼]                            │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│                                                             │
│                                                             │
│                    Chat History                             │
│ (empty)                                                     │
│                                                             │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Message: [Type a message or use a preset prompt...]    │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ [Show activity] [Clear]                                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Screen 2: After Selecting Category

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│ ### Quick Prompts                                           │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Category: [▼ Update Dataset ▼]                          │ │
│ │ Prompt:   [▼ Update Business Unit...     ▼]             │ │
│ │           [▼ Update Email Alert...       ▼]             │ │
│ │           [▼ Update Dataset Definitions...▼]            │ │
│ │           [▼ Update LinkId...            ▼]             │ │
│ │           [▼ Update Data Domain Tagging..▼]             │ │
│ │           [▼ Update Sub-Domain Tagging...▼]             │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
```

### Screen 3: After Selecting Prompt - Form Appears

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Category: [▼ Update Dataset ▼]                          │ │
│ │ Prompt:   [▼ Update Business Unit ▼]                    │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓ │
│ ┃ Configure: Update Business Unit                       ┃ │
│ ┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫ │
│ ┃                                                       ┃ │
│ ┃ Dataset Name*                                         ┃ │
│ ┃ ┌────────────────────────────────────────────────┐  ┃ │
│ ┃ │ e.g., ds_conn_s3_x                             │  ┃ │
│ ┃ └────────────────────────────────────────────────┘  ┃ │
│ ┃                                                       ┃ │
│ ┃ Current Business Unit                                 ┃ │
│ ┃ ┌────────────────────────────────────────────────┐  ┃ │
│ ┃ │ e.g., Sales                                    │  ┃ │
│ ┃ └────────────────────────────────────────────────┘  ┃ │
│ ┃                                                       ┃ │
│ ┃ New Business Unit*                                    ┃ │
│ ┃ ┌────────────────────────────────────────────────┐  ┃ │
│ ┃ │ e.g., Marketing                               │  ┃ │
│ ┃ └────────────────────────────────────────────────┘  ┃ │
│ ┃                                                       ┃ │
│ ┃ [Use This Prompt]  [Clear Form]                       ┃ │
│ ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛ │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Message: [Type a message or use a preset prompt...]    │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
```

### Screen 4: After Filling Form and Clicking "Use This Prompt"

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Category: [▼ Update Dataset ▼]                          │ │
│ │ Prompt:   [▼ Update Business Unit ▼]                    │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓ │
│ ┃ Configure: Update Business Unit                       ┃ │
│ ┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫ │
│ ┃                                                       ┃ │
│ ┃ Dataset Name*                                         ┃ │
│ ┃ ┌────────────────────────────────────────────────┐  ┃ │
│ ┃ │ ds_customer_data                               │  ┃ │
│ ┃ └────────────────────────────────────────────────┘  ┃ │
│ ┃                                                       ┃ │
│ ┃ Current Business Unit                                 ┃ │
│ ┃ ┌────────────────────────────────────────────────┐  ┃ │
│ ┃ │ Sales                                          │  ┃ │
│ ┃ └────────────────────────────────────────────────┘  ┃ │
│ ┃                                                       ┃ │
│ ┃ New Business Unit*                                    ┃ │
│ ┃ ┌────────────────────────────────────────────────┐  ┃ │
│ ┃ │ Marketing                                      │  ┃ │
│ ┃ └────────────────────────────────────────────────┘  ┃ │
│ ┃                                                       ┃ │
│ ┃ [Use This Prompt]  [Clear Form]                       ┃ │
│ ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛ │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Message: I want to update the business unit for        │ │
│ │ dataset 'ds_customer_data' from 'Sales' to             │ │
│ │ 'Marketing'.                                           │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ ← Form resets after clicking button, message auto-filled  →│
│                                                             │
```

### Screen 5: Message Ready to Submit

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Category: [▼ Select Category ▼]                         │ │
│ │ Prompt:   [▼ Select Prompt ▼]                           │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│  ← Form is hidden again after submission                  →│
│                                                             │
│                    Chat History                             │
│                                                             │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Message: I want to update the business unit for        │ │
│ │ dataset 'ds_customer_data' from 'Sales' to             │ │
│ │ 'Marketing'.                                           │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ [✓ Show activity] [Clear]  [Submit Button or Enter Key] │ │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Screen 6: After Submission (Agent Processing)

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Category: [▼ Select Category ▼]                         │ │
│ │ Prompt:   [▼ Select Prompt ▼]                           │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│                    Chat History                             │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ User: I want to update the business unit for dataset  │ │
│ │ 'ds_customer_data' from 'Sales' to 'Marketing'.        │ │
│ │                                                        │ │
│ │ Agent: Preparing request...                           │ │
│ │        Model initialized.                             │ │
│ │        Evaluating next action...                       │ │
│ │        Waiting for tool: get_dataset_definition...    │ │
│ │        Running tool: update_business_unit             │ │
│ │        Input: {"dataset": "ds_customer_data", ...}    │ │
│ │        Writing response...                             │ │
│ │        Successfully updated the business unit for     │ │
│ │        dataset 'ds_customer_data' from 'Sales' to     │ │
│ │        'Marketing'. The change has been applied       │ │
│ │        immediately. [Details...]                       │ │
│ └────────────────────────────────────────────────────────┘ │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Message: []                                             │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ [✓ Show activity] [Clear]                                │ │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Key UI Elements

### Category Dropdown
- **On Empty**: "Select Category"
- **On Click**: Shows 6 categories:
  - Create Dataset
  - Custom Rules
  - Information
  - JIRA Operations
  - Query & Inspect
  - Update Dataset

### Prompt Dropdown
- **On Empty**: "Select Prompt"
- **After Category Selection**: Shows 1-6 prompts for that category
- **Format**: "[Title] - [Description]"
- **Example**: "Update Business Unit - Modify the business unit mapping for a dataset"

### Form Fields
- **Field Count**: 1-5 fields per prompt (visible based on selection)
- **Required Marker**: Asterisk (*) after label
- **Label**: Clear description of what to enter
- **Placeholder**: Example value or format
- **Input Type**: 
  - Single-line text (most fields)
  - Multi-line textarea (descriptions, SQL)
- **Height**: Adjusts based on field type (1 line for text, 3 for textarea)

### Buttons
- **[Use This Prompt]** (Primary/Blue)
  - Renders template and populates message field
  - Clears form after clicking
  
- **[Clear Form]** (Secondary)
  - Resets all form fields
  - Hides form if no category selected
  - Resets dropdowns

### Form Visibility
- **Hidden**: On initial page load
- **Hidden**: After form submission
- **Shown**: After valid prompt selection
- **Hidden**: After clicking Clear/Use buttons (unless prompt still selected)

## Error States

### Missing Required Field
```
Message: ⚠️  Missing required fields: dataset_name, new_bu

Please fill in all fields marked with *.
```

### Invalid JSON Configuration
```
Message: Error: No template selected
```

## Responsive Design

### Desktop (1024px+)
- All elements visible in 2-column layout
- Form and chat side by side (optional enhancement)

### Tablet (768px-1024px)
- Single column, form above chat
- Form expands full width when active

### Mobile (< 768px)
- Single column, form above chat
- Form scrollable
- Buttons stack vertically

## Accessibility Features

- ✅ Form labels associated with inputs
- ✅ Required fields marked with asterisk (*)
- ✅ Required field names in error messages
- ✅ Placeholder text for guidance (not substitute for label)
- ✅ Button labels clear and descriptive
- ✅ High contrast: blue buttons, clear text
- ⚠️ Could add: ARIA labels, screen reader testing

## Animation/Interaction Feedback

- **On Category Change**: Dropdown updates instantly
- **On Prompt Change**: Form fades in (Gradio default)
- **On Field Focus**: Standard text input focus styling
- **On Click Submit**: Form briefly disabled while processing
- **After Submit**: Form resets with smooth transition

## Future UI Enhancements

1. **Search**: Add search box to find prompts
2. **History**: Show recently used prompts
3. **Favorites**: Star/save favorite prompt templates
4. **Preview**: Show rendered prompt before submission
5. **Undo**: Undo last used prompt
6. **Keyboard Shortcuts**: Alt+P to open prompts panel
7. **Collapsible Form**: Minimize form to see chat better
8. **Help Tooltips**: Hover help for each field

