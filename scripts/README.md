# Scripts Directory Path Requirements

This document outlines the key path requirements and directory structure needed for the PDDL planning system.

## Directory Structure

```
scripts/
├── pddlrun_llmseparate.py    # Main PDDL planning script
├── execute_plan.py           # Plan execution script
└── ai2_thor_controller.py    # AI2Thor controller script

resources/
├── actions.py               # Action definitions
├── robots.py               # Robot configurations
├── generated_subtask/      # Generated PDDL subtasks
└── *.pddl                  # PDDL domain files

data/
├── aithor_connect/         # AI2Thor connection utilities
├── pythonic_plans/         # Example PDDL plans
└── final_test/            # Test data

downward/                   # PDDL planner
└── fast-downward.py       # Fast Downward planner executable

logs/                      # Generated logs and results
```

## Key Path Requirements

### 1. Base Directory Structure
- The system expects to be run from the root directory of the project
- All relative paths are constructed using `os.getcwd()`
- Required directories must exist or be created:
  - `resources/`
  - `resources/generated_subtask/`
  - `logs/`
  - `data/`
  - `downward/`

### 2. Resource Files
- PDDL domain files must be present in `resources/`
- Required files:
  - `allactionrobot.pddl`
  - `robot1.pddl`, `robot2.pddl`, etc. (based on available robots)
  - `actions.py` with action definitions
  - `robots.py` with robot configurations

### 3. Planner Requirements
- Fast Downward planner must be installed in the `downward/` directory
- The planner executable must be accessible at `downward/fast-downward.py`

### 4. Test Data
- Test data should be placed in `data/final_test/`
- Expected format: JSON files named `FloorPlan{N}.json`
- Each JSON file should contain:
  - `task`: Task description
  - `robot list`: List of robot IDs
  - `object_states`: Ground truth object states
  - `trans`: Transaction counts
  - `max_trans`: Maximum transaction counts

### 5. Prompt Template
- Template should be in `data/pythonic_plans/`
- Required files:
  - `pddl_train_task_decomposesep_teamproblem.py`
  - `pddl_train_task_decomposesep_problem.py`
  - `pddl_train_task_allocationsep_teamproblem.py`
  - `pddl_train_task_allocationsep_problem.py`

### 6. API Key
- OpenAI API key file should be named `api_key.txt`
- Default location: root directory
- Can be specified via command line argument `--openai-api-key-file`

## Command Line Arguments

```bash
python pddlrun_llmseparate.py \
    --floor-plan <number> \
    --openai-api-key-file <path> \
    --model <model> \
    --prompt-decompse-set <set> \
    --prompt-allocation-set <set> \
    --test-set <set> \
    --log-results <boolean>
```

### Plan-to-Code 


```bash
# Enable validation (default)
python plantocode.py --logs-dir ./logs --validate-code

# Disable validation for faster processing
python plantocode.py --logs-dir ./logs --no-validate-code
```


## Error Handling

The system will raise the following exceptions if path requirements are not met:
- `PDDLError`: For PDDL-related file operations
- `ValidationError`: For PDDL validation issues
- `PlanningError`: For planner execution issues
- `LLMError`: For API and model-related issues

## Notes

- All paths are constructed relative to the current working directory
- The system will attempt to create missing directories when possible
- File permissions must allow read/write access to all required directories
- Temporary files are stored in `resources/generated_subtask/`
- Results and logs are saved in `logs/` with timestamp-based subdirectories 