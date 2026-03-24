
import json
import argparse
import os
import re
import sys
import glob
import yaml
from pathlib import Path
from datetime import datetime
import time
from typing import Dict, List, Any, Optional, Union, Tuple

from openai import OpenAI
import openai

from llm_logger import log_llm_call, get_llm_logger

# Constants
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.1
DEFAULT_RETRY_DELAY = 20
MAX_RETRIES = 3

def get_available_models():
    """Get list of available models from providers.yaml"""
    providers_file = Path(__file__).parent / 'providers.yaml'
    with open(providers_file, 'r', encoding='utf-8') as f:
        providers = yaml.safe_load(f)['providers']
    return [model for provider in providers for model in provider['models']]

class LLMError(Exception):
    """Exception raised for Language Model related errors."""
    pass

class MimicTranslationError(Exception):
    """Exception raised for Mimic translation errors."""
    pass

class LLMHandler:
    """Handles interactions with Language Models (LLMs) using OpenAI API."""
    
    def __init__(self):
        """Initialize the LLM handler."""
        self.clients = {}
        self.providers = None
    
    def _load_providers(self):
        """Load providers from yaml file."""
        if self.providers is None:
            providers_file = Path(__file__).parent / 'providers.yaml'
            with open(providers_file, 'r', encoding='utf-8') as f:
                self.providers = yaml.safe_load(f)['providers']
        return self.providers
    
    def _get_client_for_model(self, model):
        """Get or create OpenAI client for the provider of the given model."""
        for provider in self._load_providers():
            if model in provider['models']:
                provider_name = provider['name']
                if provider_name not in self.clients:
                    self.clients[provider_name] = OpenAI(
                        api_key=provider['api_key'],
                        base_url=provider['base_url']
                    )
                return self.clients[provider_name], provider_name
        raise LLMError(f"Model {model} not found in any provider")
    
    def query_model(
        self, 
        prompt: Union[str, List[Dict]], 
        model: str, 
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        stop: Optional[List[str]] = None,
        logprobs: Optional[int] = 1,
        frequency_penalty: float = 0
    ) -> Tuple[dict, str]:
        """Query the language model using OpenAI API.
        """
        retry_delay = DEFAULT_RETRY_DELAY
        client, provider = self._get_client_for_model(model)
        
        for attempt in range(MAX_RETRIES):
            try:
                start_time = time.time()
                response = client.chat.completions.create(
                    model=model, 
                    messages=prompt, 
                    max_tokens=max_tokens, 
                    temperature=temperature, 
                    frequency_penalty=frequency_penalty
                )
                duration_ms = (time.time() - start_time) * 1000
                text = response.choices[0].message.content.strip()
                
                usage = None
                if hasattr(response, 'usage') and response.usage is not None:
                    usage = {
                        'prompt_tokens': response.usage.prompt_tokens,
                        'completion_tokens': response.usage.completion_tokens,
                        'total_tokens': response.usage.total_tokens
                    }
                
                log_llm_call(
                    model=model,
                    provider=provider,
                    messages=prompt if isinstance(prompt, list) else [{'role': 'user', 'content': prompt}],
                    params={
                        'max_tokens': max_tokens,
                        'temperature': temperature,
                        'frequency_penalty': frequency_penalty
                    },
                    response_text=text,
                    usage=usage,
                    duration_ms=duration_ms
                )
                
                return response, text
                    
            except openai.RateLimitError:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                    continue
                raise LLMError("Rate limit exceeded")
                
            except (openai.APIError, openai.APITimeoutError) as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(retry_delay)
                    continue
                raise LLMError(f"API Error after all retries: {str(e)}")
                
            except Exception as e:
                raise LLMError(f"Unexpected error in LLM query: {str(e)}")

class MimicFormatTranslator:
    """Translates complete PDDL plans to mimic format using OpenAI API."""
    
    def __init__(self, model: str = "MiniMax-M2.7"):
        self.model = model
        self.llm = LLMHandler()
        print(f"Initialized MimicFormatTranslator with {model}")
    
    def validate_mimic_code(self, mimic_code: str, task_description: str) -> Tuple[bool, str]:
        """Validate if the generated mimic code would be executable by execute_plan.py.
        
        Args:
            mimic_code (str): The generated mimic code to validate
            task_description (str): Description of the task for context
            
        Returns:
            Tuple[bool, str]: (is_valid, validation_message)
        """
        try:
            # Create validation prompt
            validation_prompt = f"""You are a Python code validator for AI2-THOR robot execution. 
Your task is to validate if the following code would be executable by execute_plan.py.

Context: This code is generated from a PDDL plan for the task: "{task_description}"

Available AI2-THOR functions (assume these are imported and available):
- GoToObject(robot, object_name)
- PickupObject(robot, object_name) 
- PutObject(robot, object_name, target_location)
- SwitchOn(robot, object_name)
- SwitchOff(robot, object_name)
- time.sleep(seconds)

Available variables (assume these are defined):
- robots: list of robot objects [robots[0], robots[1], etc.]
- action_queue: list for tracking actions
- task_over: boolean flag

Validation criteria:
1. All function calls must use valid AI2-THOR functions
2. All robot parameters must reference robots list (e.g., robots[0], robots[1])
3. Function parameters should be 'robots' (not 'robot') and access robots[0], robots[1], etc.
4. Threading must be properly structured with start() and join()
5. Action queue must be properly managed
6. No undefined variables or functions
7. Proper Python syntax

Generated code to validate:
{mimic_code}

Please analyze this code and respond with:
1. VALID: true/false
2. ISSUES: List any issues found (empty if valid)
3. SUGGESTIONS: How to fix any issues (empty if valid)

Format your response exactly like this:
VALID: true
ISSUES: 
SUGGESTIONS: 

Or if there are issues:
VALID: false
ISSUES: 
- Issue 1 description
- Issue 2 description
SUGGESTIONS:
- Fix 1: specific suggestion
- Fix 2: specific suggestion
"""

            # Query the model for validation - use proper message format for models
            messages = [
                {"role": "system", "content": "You are a Python code validator for AI2-THOR robot execution. Your task is to validate if code would be executable by execute_plan.py."},
                {"role": "user", "content": validation_prompt}
            ]
            _, validation_response = self.llm.query_model(
                prompt=messages,
                model=self.model,
                max_tokens=512,
                temperature=0.0,  # Use 0 temperature for consistent validation
                frequency_penalty=0.0
            )
            
            # Parse validation response
            is_valid = False
            issues = []
            suggestions = []
            
            lines = validation_response.strip().split('\n')
            for line in lines:
                if line.startswith('VALID:'):
                    is_valid = line.split(':', 1)[1].strip().lower() == 'true'
                elif line.startswith('ISSUES:'):
                    # Collect all issue lines until we hit SUGGESTIONS
                    continue
                elif line.startswith('SUGGESTIONS:'):
                    # Collect all suggestion lines
                    continue
                elif line.strip().startswith('-') and 'ISSUES:' in validation_response:
                    # This is an issue line
                    if 'SUGGESTIONS:' not in validation_response or validation_response.find('ISSUES:') < validation_response.find('SUGGESTIONS:'):
                        issues.append(line.strip()[1:].strip())
                elif line.strip().startswith('-') and 'SUGGESTIONS:' in validation_response:
                    # This is a suggestion line
                    if validation_response.find('SUGGESTIONS:') < validation_response.find(line):
                        suggestions.append(line.strip()[1:].strip())
            
            # Create validation message
            if is_valid:
                validation_message = "✓ Code validation passed - executable by execute_plan.py"
            else:
                validation_message = f"✗ Code validation failed:\n"
                if issues:
                    validation_message += "Issues found:\n"
                    for issue in issues:
                        validation_message += f"  - {issue}\n"
                if suggestions:
                    validation_message += "Suggestions:\n"
                    for suggestion in suggestions:
                        validation_message += f"  - {suggestion}\n"
            
            return is_valid, validation_message
            
        except Exception as e:
            return False, f"Validation error: {str(e)}"
    
    def validate_and_fix_mimic_code(self, mimic_code: str, task_description: str) -> Tuple[bool, str, str]:
        """Validate and fix the generated mimic code to match the AI2-THOR template.
        
        Args:
            mimic_code (str): The generated mimic code to validate and fix
            task_description (str): Description of the task for context
            
        Returns:
            Tuple[bool, str, str]: (is_valid, validation_message, corrected_code)
        """
        try:
            # Create validation and fixing prompt
            fix_prompt = f"""You are a Python code validator and fixer for AI2-THOR robot execution. 
Your task is to validate and FIX the following code to match the AI2-THOR template structure.

Context: This code is generated from a PDDL plan for the task: "{task_description}"

CRITICAL: DO NOT REDEFINE AI2-THOR FUNCTIONS
The following AI2-THOR functions are ALREADY DEFINED and available:
- GoToObject(robot, object_name)
- PickupObject(robot, object_name) 
- PutObject(robot, object_name, target_location)
- SwitchOn(robot, object_name)
- SwitchOff(robot, object_name)
- time.sleep(seconds)

DO NOT create new function definitions for these. Use them directly as shown in the template.
DO NOT add "def GoToObject(...):" or similar definitions.

Required template structure:
1. Functions should take 'robots' parameter and use robots[0], robots[1], etc.
2. Use proper threading for parallel execution
3. Include action_queue management
4. Use task_over flag
5. Follow this exact structure:

def task_function(robots):
    # Task description
    GoToObject(robots[0], 'Object')
    PickupObject(robots[0], 'Object')
    # ... more actions

# Threading setup
task1_thread = threading.Thread(target=task_function, args=(robots,))
task1_thread.start()
task1_thread.join()

# Action queue and completion
action_queue.append({{'action':'Done'}})
task_over = True
time.sleep(5)

Generated code to fix:
{mimic_code}

Please analyze this code and:
1. Fix all issues to match the AI2-THOR template
2. Ensure proper robot parameter usage (robots[0], robots[1])
3. Add proper threading structure if missing
4. Add action_queue and task_over management
5. Remove any invalid AI2-THOR functions and replace with valid ones
6. REMOVE any function definitions for GoToObject, PickupObject, PutObject, etc. - these are already available

Return ONLY the corrected code that follows the template structure exactly.
"""

            messages = [
                {"role": "system", "content": "You are a Python code fixer for AI2-THOR robot execution. Fix the code to match the exact template structure."},
                {"role": "user", "content": fix_prompt}
            ]
            _, corrected_code = self.llm.query_model(
                prompt=messages,
                model=self.model,
                max_tokens=2048,
                temperature=0.0,
                frequency_penalty=0.0
            )
            
            # Clean up the corrected code (remove markdown if present)
            corrected_code = corrected_code.strip()
            if corrected_code.startswith('```python'):
                corrected_code = corrected_code[9:]
            if corrected_code.endswith('```'):
                corrected_code = corrected_code[:-3]
            corrected_code = corrected_code.strip()
            
            # Validate the corrected code
            is_valid, validation_message = self.validate_mimic_code(corrected_code, task_description)
            
            return is_valid, validation_message, corrected_code
            
        except Exception as e:
            return False, f"Validation and fixing error: {str(e)}", mimic_code
    
    def create_few_shot_prompt(self, task_description: str, combined_plan: str) -> Union[str, List[Dict]]:
        # Few-shot examples for complete plan translation
        few_shot_examples = f"""# CRITICAL INSTRUCTION: DO NOT REDEFINE AI2-THOR FUNCTIONS
# The following AI2-THOR functions are ALREADY DEFINED and available:
# - GoToObject(robot, object_name)
# - PickupObject(robot, object_name) 
# - PutObject(robot, object_name, target_location)
# - SwitchOn(robot, object_name)
# - SwitchOff(robot, object_name)
# - time.sleep(seconds)
# 
# DO NOT create new function definitions for these. Use them directly as shown in the template.
# DO NOT add "def GoToObject(...):" or similar definitions.

# Example: Complete PDDL Plan Translation with Multi-Robot Coordination
Task: Wash multiple vegetables (apple, tomato, lettuce, potato)
Complete PDDL Plan: (define (problem wash_vegetables) (:domain robot_domain) (:objects apple tomato lettuce potato sink faucet counter) (:init (at apple counter) (at tomato counter) (at lettuce counter) (at potato counter)) (:goal (and (washed apple) (washed tomato) (washed lettuce) (washed potato))))

# IMPORTANT: Follow this EXACT structure for AI2-THOR execution
# NOTE: AI2-THOR functions are already imported and available - DO NOT redefine them

def wash_apple(robots):
    # 0: Task: Wash the Apple
    # 1: Go to the Apple.
    GoToObject(robots[0], 'Apple')
    # 2: Pick up the Apple.
    PickupObject(robots[0], 'Apple')
    # 3: Go to the Sink.
    GoToObject(robots[0], 'Sink')
    # 4: Put the Apple in the Sink.
    PutObject(robots[0], 'Apple', 'Sink')
    # 5: Switch on the Faucet.
    SwitchOn(robots[0], 'Faucet')
    # 6: Wait for a while to let the Apple wash.
    time.sleep(5)
    # 7: Switch off the Faucet.
    SwitchOff(robots[0], 'Faucet')
    # 8: Pick up the washed Apple.
    PickupObject(robots[0], 'Apple')
    # 9: Go to the CounterTop.
    GoToObject(robots[0], 'CounterTop')
    # 10: Put the washed Apple on the CounterTop.
    PutObject(robots[0], 'Apple', 'CounterTop')

def wash_tomato(robots):
    # 0: Task: Wash the Tomato
    # 1: Go to the Tomato.
    GoToObject(robots[1], 'Tomato')
    # 2: Pick up the Tomato.
    PickupObject(robots[1], 'Tomato')
    # 3: Go to the Sink.
    GoToObject(robots[1], 'Sink')
    # 4: Put the Tomato in the Sink.
    PutObject(robots[1], 'Tomato', 'Sink')
    # 5: Switch on the Faucet.
    SwitchOn(robots[1], 'Faucet')
    # 6: Wait for a while to let the Tomato wash.
    time.sleep(5)
    # 7: Switch off the Faucet.
    SwitchOff(robots[1], 'Faucet')
    # 8: Pick up the washed Tomato.
    PickupObject(robots[1], 'Tomato')
    # 9: Go to the CounterTop.
    GoToObject(robots[1], 'CounterTop')
    # 10: Put the washed Tomato on the CounterTop.
    PutObject(robots[1], 'Tomato', 'CounterTop')

def wash_lettuce(robots):
    # 0: Task: Wash the Lettuce
    # 1: Go to the Lettuce.
    GoToObject(robots[0], 'Lettuce')
    # 2: Pick up the Lettuce.
    PickupObject(robots[0], 'Lettuce')
    # 3: Go to the Sink.
    GoToObject(robots[0], 'Sink')
    # 4: Put the Lettuce in the Sink.
    PutObject(robots[0], 'Lettuce', 'Sink')
    # 5: Switch on the Faucet.
    SwitchOn(robots[0], 'Faucet')
    # 6: Wait for a while to let the Lettuce wash.
    time.sleep(5)
    # 7: Switch off the Faucet.
    SwitchOff(robots[0], 'Faucet')
    # 8: Pick up the washed Lettuce.
    PickupObject(robots[0], 'Lettuce')
    # 9: Go to the CounterTop.
    GoToObject(robots[0], 'CounterTop')
    # 10: Put the washed Lettuce on the CounterTop.
    PutObject(robots[0], 'Lettuce', 'CounterTop')

def wash_potato(robots):
    # 0: Task: Wash the Potato
    # 1: Go to the Potato.
    GoToObject(robots[1], 'Potato')
    # 2: Pick up the Potato.
    PickupObject(robots[1], 'Potato')
    # 3: Go to the Sink.
    GoToObject(robots[1], 'Sink')
    # 4: Put the Potato in the Sink.
    PutObject(robots[1], 'Potato', 'Sink')
    # 5: Switch on the Faucet.
    SwitchOn(robots[1], 'Faucet')
    # 6: Wait for a while to let the Potato wash.
    time.sleep(5)
    # 7: Switch off the Faucet.
    SwitchOff(robots[1], 'Faucet')
    # 8: Pick up the washed Potato.
    PickupObject(robots[1], 'Potato')
    # 9: Go to the CounterTop.
    GoToObject(robots[1], 'CounterTop')
    # 10: Put the washed Potato on the CounterTop.
    PutObject(robots[1], 'Potato', 'CounterTop')

# CRITICAL: Robot task allocation and threading structure
# Assign tasks to robots based on their skills
# Parallelize all tasks
# Assign Task1 to robot1 since it has all the skills to perform actions in Task 1
task1_thread = threading.Thread(target=wash_apple, args=(robots,))
# Assign Task2 to robot2 since it has all the skills to perform actions in Task 2
task2_thread = threading.Thread(target=wash_tomato, args=(robots,))

# Start executing Task 1 and Task 2 in parallel
task1_thread.start()
task2_thread.start()

# Wait for both Task 1 and Task 2 to finish
task1_thread.join()
task2_thread.join()

# Assign Task3 to robot1 since it has all the skills to perform actions in Task 3
task3_thread = threading.Thread(target=wash_lettuce, args=(robots,))
# Assign Task4 to robot2 since it has all the skills to perform actions in Task 4
task4_thread = threading.Thread(target=wash_potato, args=(robots,))

# Start executing Task 3 and Task 4 in parallel
task3_thread.start()
task4_thread.start()

# Wait for both Task 3 and Task 4 to finish
task3_thread.join()
task4_thread.join()

# Task wash_apple, wash_tomato, wash_lettuce, wash_potato is done
action_queue.append({{'action':'Done'}})
action_queue.append({{'action':'Done'}})
action_queue.append({{'action':'Done'}})

task_over = True
time.sleep(5)

# Now translate the following complete plan:
Task: {task_description}
Complete PDDL Plan: {combined_plan}

# IMPORTANT: Generate code that follows the EXACT structure above
def execute_task():
    # Complete plan execution for: {task_description}
"""
        
        return [
            {"role": "system", "content": "You are a Robot PDDL to Mimic Format Translator. Your task is to translate complete PDDL plans into executable Python code following the AI2-THOR controller format. Translate the entire plan as a single coherent function."},
            {"role": "user", "content": few_shot_examples}
        ]
    
    def translate_to_mimic_format(self, task_description: str, combined_plan: str,
                                max_tokens: int = 2048,  # Increased for complete plans
                                temperature: float = 0.1,
                                frequency_penalty: float = 0.0) -> str:
        """Translate complete PDDL plan to mimic format using OpenAI API."""
        try:
            # Create few-shot prompt
            prompt = self.create_few_shot_prompt(task_description, combined_plan)
            
            # Query the model
            start_time = time.time()
            _, response = self.llm.query_model(
                prompt=prompt,
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                frequency_penalty=frequency_penalty
            )
            translation_time = time.time() - start_time
            
            print(f"Translation completed in {translation_time:.2f}s")
            return response
            
        except Exception as e:
            raise MimicTranslationError(f"Error in mimic translation: {str(e)}")
    
    def extract_function_name(self, task_description: str) -> str:
        """Extract a function name from task description."""
        clean_task = re.sub(r'[^a-zA-Z0-9\s]', '', task_description.lower())
        words = clean_task.split()[:3]  # Take first 3 words
        function_name = '_'.join(words)
        return function_name

def load_pddl_results_from_logs(logs_dir: str) -> List[Dict[str, Any]]:
    """Load PDDL results from the log directories created by pddlrun_llmseparate.py."""
    
    results = []
    logs_path = Path(logs_dir)
    
    if not logs_path.exists():
        print(f"Logs directory not found: {logs_dir}")
        return results
    
    # Find all log folders (they end with _plans_YYYY-MM-DD-HH-MM-SS)
    log_folders = list(logs_path.glob("*_plans_*"))
    
    if not log_folders:
        print(f"No log folders found in {logs_dir}")
        return results
    
    print(f"Found {len(log_folders)} log folders")
    
    for folder in log_folders:
        try:
            # Read log.txt to get task information
            log_file = folder / "log.txt"
            if not log_file.exists():
                print(f"Warning: log.txt not found in {folder}")
                continue
            
            with open(log_file, 'r', encoding='utf-8') as f:
                log_content = f.read()
            
            # Extract task description from log content
            lines = log_content.split('\n')
            task_description = lines[0] if lines else "Unknown task"
            
            # Read the COMBINED plan (code_planpddl.py) - this contains the complete executable plan
            combined_plan_file = folder / "code_planpddl.py"
            if not combined_plan_file.exists():
                print(f"Warning: code_planpddl.py not found in {folder}")
                continue
            
            with open(combined_plan_file, 'r', encoding='utf-8') as f:
                combined_plan_content = f.read()
            
            # Create result entry with the complete plan
            result = {
                'episode_id': folder.name,
                'scene_id': 'pddl_generated',
                'task_description': task_description,
                'combined_plan': combined_plan_content,  # Store the complete plan
                'log_folder': str(folder)
            }
            
            results.append(result)
            print(f"  ✓ Loaded: {folder.name} - {task_description[:50]}...")
            print(f"    Plan length: {len(combined_plan_content)} characters")
            
        except Exception as e:
            print(f"Error processing folder {folder}: {e}")
            continue
    
    print(f"Successfully loaded {len(results)} PDDL results")
    return results

def load_extraction_results(results_file: str) -> List[Dict[str, Any]]:
    """Load results from JSON file (for backward compatibility)."""
    try:
        with open(results_file, 'r', encoding='utf-8') as f:
            results = json.load(f)
        print(f"Loaded {len(results)} results from {results_file}")
        return results
    except Exception as e:
        print(f"Error loading results file {results_file}: {e}")
        return []

def process_results_for_plan_to_code(results: List[Dict[str, Any]], translator: MimicFormatTranslator,
                            output_dir: str, batch_size: int = 3, validate_code: bool = True) -> List[Dict[str, Any]]:
    """Process all results to translate to plan-to-code format."""
    processed_results = []
    
    print(f"Processing {len(results)} results for plan-to-code translation...")
    print(f"Processing in batches of {batch_size}")
    if validate_code:
        print("Code validation enabled - checking execute_plan.py compatibility")
    else:
        print("Code validation disabled")
    
    # Process in batches to manage API rate limits
    for batch_start in range(0, len(results), batch_size):
        batch_end = min(batch_start + batch_size, len(results))
        batch_results = results[batch_start:batch_end]
        
        print(f"\nProcessing batch {batch_start//batch_size + 1}/{(len(results) + batch_size - 1)//batch_size}")
        print(f"Tasks {batch_start + 1}-{batch_end}")
        
        for i, result in enumerate(batch_results):
            global_index = batch_start + i
            episode_id = result.get('episode_id', f'task_{global_index}')
            scene_id = result.get('scene_id', 'unknown')
            task_description = result.get('task_description', '')
            combined_plan = result.get('combined_plan', '')
            
            print(f"\n[{global_index + 1}/{len(results)}] Processing Episode {episode_id}, Scene {scene_id}")
            
            try:
                # Skip if no combined plan was loaded
                if not combined_plan or combined_plan.strip() == "":
                    print(f"  ⚠ No combined plan found, skipping")
                    processed_result = {
                        'episode_id': episode_id,
                        'scene_id': scene_id,
                        'task_description': task_description,
                        'original_combined_plan': combined_plan,
                        'mimic_format_code': None,
                        'function_name': None,
                        'translation_time': 0,
                        'success': False,
                        'error': 'No combined plan to translate',
                        'validation_message': 'Skipped - no plan to validate',
                        'log_folder': result.get('log_folder', '')  # Add the log_folder to processed_result
                    }
                    processed_results.append(processed_result)
                    continue
                
                # Translate to mimic format using OpenAI API
                start_time = time.time()
                mimic_code = translator.translate_to_mimic_format(task_description, combined_plan)
                translation_time = time.time() - start_time
                
                # Extract function name
                function_name = translator.extract_function_name(task_description)
                
                # Validate and fix the generated mimic code if enabled
                if validate_code:
                    print(f"  🔧 Validating and fixing generated code...")
                    try:
                        is_valid, validation_message, corrected_code = translator.validate_and_fix_mimic_code(mimic_code, task_description)
                        # Use the corrected code instead of the original
                        if corrected_code and len(corrected_code.strip()) > 0:
                            mimic_code = corrected_code
                            print(f"  📝 Original code length: {len(mimic_code) if mimic_code else 0}")
                            print(f"  📝 Corrected code length: {len(corrected_code) if corrected_code else 0}")
                            print(f"  ✅ Validation result: {is_valid}")
                        else:
                            print(f"  ⚠ Validation returned empty code, using original")
                            is_valid = True
                            validation_message = "Using original code - validation returned empty"
                    except Exception as e:
                        print(f"  ⚠ Validation failed: {e}, using original code")
                        is_valid = True
                        validation_message = f"Using original code - validation error: {str(e)}"
                else:
                    is_valid = True
                    validation_message = "Validation skipped"
                
                # Create processed result
                processed_result = {
                    'episode_id': episode_id,
                    'scene_id': scene_id,
                    'task_description': task_description,
                    'original_combined_plan': combined_plan,
                    'mimic_format_code': mimic_code,
                    'function_name': function_name,
                    'translation_time': translation_time,
                    'extraction_time': result.get('extraction_time', 0),
                    'generation_time': result.get('generation_time', 0),
                    'success': len(mimic_code.strip()) > 0 if mimic_code else False, # Consider successful if we have code, regardless of validation
                    'validation_passed': is_valid,  # Track validation status separately
                    'validation_message': validation_message,
                    'log_folder': result.get('log_folder', '')  # Add the log_folder to processed_result
                }
                
                processed_results.append(processed_result)
                
                print(f"  ✓ Translated to mimic format ({translation_time:.2f}s)")
                print(f"  Function name: {function_name}")
                print(f"  Code preview: {mimic_code[:100]}{'...' if len(mimic_code) > 100 else ''}")
                # Remove validation message printing but keep validation functionality
                
            except Exception as e:
                print(f"  ✗ Error in mimic translation: {e}")
                processed_result = {
                    'episode_id': episode_id,
                    'scene_id': scene_id,
                    'task_description': task_description,
                    'original_combined_plan': combined_plan,
                    'error': str(e),
                    'success': False,
                    'validation_message': f'Error during translation: {str(e)}',
                    'log_folder': result.get('log_folder', '')  # Add the log_folder to processed_result
                }
                processed_results.append(processed_result)
        
        # Add delay between batches to respect API rate limits
        if batch_start + batch_size < len(results):
            print(f"  Waiting 2 seconds before next batch...")
            time.sleep(2)
    
    return processed_results

def save_individual_plan_to_code_files(processed_results: List[Dict[str, Any]], output_dir: str):
    """Save individual plan-to-code format files directly in the original log folders."""
    
    successful_translations = [r for r in processed_results if r.get('success', False)]
    
    print(f"\nSaving {len(successful_translations)} plan-to-code files in original log folders...")
    
    for i, result in enumerate(successful_translations):
        episode_id = result.get('episode_id', f'task_{i}')
        scene_id = result.get('scene_id', 'unknown')
        function_name = result.get('function_name', f'task_{i}')
        plan_to_code = result.get('mimic_format_code', '')
        task_description = result.get('task_description', '')
        log_folder = result.get('log_folder', '')
        validation_passed = result.get('validation_passed', False)
        validation_message = result.get('validation_message', '')
        
        print(f"  📁 Processing: {episode_id}")
        print(f"  📝 Code length: {len(plan_to_code) if plan_to_code else 0}")
        print(f"  📂 Log folder: {log_folder}")
        print(f"  ✅ Validation passed: {validation_passed}")
        if not validation_passed:
            print(f"  ⚠ Validation issues: {validation_message[:100]}...")
        
        if log_folder and Path(log_folder).exists():
            original_log_path = Path(log_folder)
            
            # Check if we have valid code to save
            if not plan_to_code or len(plan_to_code.strip()) == 0:
                print(f"  ⚠ No valid code to save for {episode_id}")
                continue
            
            # Create plan_to_code subdirectory in the original log folder
            plan_to_code_dir = original_log_path / "plan_to_code"
            plan_to_code_dir.mkdir(exist_ok=True)
            
            # Save the plan-to-code file in the log folder
            filename = f"plan_to_code_{function_name}.py"
            filepath = plan_to_code_dir / filename
            
            # Create complete Python file content
            file_content = f"""#!/usr/bin/env python3
\"\"\"
Plan-to-Code Format for AI2-THOR Controller
Generated from Complete PDDL Plan Translation

Episode ID: {episode_id}
Scene ID: {scene_id}
Task: {task_description}
Validation Passed: {validation_passed}
Validation Message: {validation_message}
\"\"\"

import time
import threading

# Import AI2-THOR controller functions
# from ai2thor_controller import GoToObject, PickupObject, PutObject, SwitchOn, SwitchOff

{plan_to_code}

# Example usage:
# robot = get_robot_instance()
# execute_task(robot)
"""
            
            # Save file in the log folder
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(file_content)
            
            print(f"  ✓ Saved: {filename} in {log_folder}")
            
            # Also save as code_plan.py in the main log folder for execute_plan.py compatibility
            code_plan_path = original_log_path / "code_plan.py"
            with open(code_plan_path, 'w', encoding='utf-8') as f:
                f.write(plan_to_code)
            
            print(f"  ✓ Saved code_plan.py in: {log_folder}")
            
        else:
            print(f"  ⚠ Could not save files - log folder not found: {log_folder}")
    
    print(f"All plan-to-code files saved in their respective log folders")

def generate_summary(processed_results: List[Dict[str, Any]], output_dir: str):
    """Generate summary statistics and reports."""
    total_results = len(processed_results)
    successful_translations = sum(1 for r in processed_results if r.get('success', False))
    
    # Validation statistics
    validation_results = [r.get('validation_message', '') for r in processed_results]
    validation_passed = sum(1 for msg in validation_results if '✓' in msg or 'passed' in msg.lower())
    validation_failed = sum(1 for msg in validation_results if '✗' in msg or 'failed' in msg.lower())
    validation_skipped = sum(1 for msg in validation_results if 'skipped' in msg.lower())
    
    # Time statistics
    translation_times = [r.get('translation_time', 0) for r in processed_results if r.get('translation_time')]
    avg_translation_time = sum(translation_times) / len(translation_times) if translation_times else 0
    
    # Generate summary report
    summary = {
        'total_results': total_results,
        'successful_translations': successful_translations,
        'success_rate': successful_translations / total_results * 100 if total_results > 0 else 0,
        'validation_passed': validation_passed,
        'validation_failed': validation_failed,
        'validation_skipped': validation_skipped,
        'validation_success_rate': validation_passed / (validation_passed + validation_failed) * 100 if (validation_passed + validation_failed) > 0 else 0,
        'average_translation_time': avg_translation_time,
        'total_translation_time': sum(translation_times)
    }
    
    # Save summary in output directory
    summary_file = Path(output_dir) / "plan_to_code_summary.json"
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    
    # Save detailed results in output directory
    results_file = Path(output_dir) / "plan_to_code_results.json"
    with open(results_file, 'w', encoding='utf-8') as f:
        json.dump(processed_results, f, indent=2, ensure_ascii=False)
    
    # Print summary
    print(f"\n=== PLAN-TO-CODE TRANSLATION SUMMARY ===")
    print(f"Total results processed: {total_results}")
    print(f"Successful translations: {successful_translations} ({summary['success_rate']:.1f}%)")
    # Remove validation statistics printing but keep validation functionality
    print(f"Average translation time: {summary['average_translation_time']:.2f}s")
    print(f"Total translation time: {summary['total_translation_time']:.2f}s")
    
    print(f"\nSummary files saved to: {output_dir}")
    print(f"Summary: {summary_file}")
    print(f"Detailed results: {results_file}")
    print(f"Individual plan-to-code files saved in their respective log folders")

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Translate complete PDDL plans to AI2-THOR executable code using OpenAI API. Can load from JSON files or PDDL log directories created by pddlrun_llmseparate.py')
    parser.add_argument('--model', type=str, default="MiniMax-M2.7",
                       choices=get_available_models(),
                       help='Model to use')
    parser.add_argument('--input-source', type=str, choices=['json', 'pddl_logs'], default='pddl_logs',
                       help='Input source type: json file or pddl_logs directory')
    parser.add_argument('--input-file', type=str, 
                       default='../model_testing/70b_extracted_actions/70b_extracted_action_sequences.json',
                       help='Path to the extracted action sequences JSON file (for json input source)')
    parser.add_argument('--logs-dir', type=str, 
                       default='./logs',
                       help='Path to logs directory from pddlrun_llmseparate.py (for pddl_logs input source)')
    parser.add_argument('--output-dir', type=str, 
                       default='./plan_to_code_results',
                       help='Directory to save plan-to-code translation results')
    parser.add_argument('--batch-size', type=int, default=3,
                       help='Number of tasks to process in each batch (default: 3)')
    parser.add_argument('--max-tokens', type=int, default=2048,  # Increased default
                       help='Maximum number of tokens to generate')
    parser.add_argument('--temperature', type=float, default=0.1,
                       help='Sampling temperature')
    parser.add_argument('--frequency-penalty', type=float, default=0.0,
                       help='Frequency penalty for token generation')
    parser.add_argument('--validate-code', action='store_true', default=True,
                       help='Validate generated code for execute_plan.py compatibility (default: True)')
    parser.add_argument('--no-validate-code', dest='validate_code', action='store_false',
                       help='Skip code validation')
    
    args = parser.parse_args()
    
    # Validate input based on source type
    if args.input_source == 'json' and not args.input_file:
        parser.error("--input-file must be provided for json input source")
    elif args.input_source == 'pddl_logs' and not args.logs_dir:
        parser.error("--logs-dir must be provided for pddl_logs input source")
        
    return args

def main():
    """Main execution function."""
    try:
        # Parse arguments
        args = parse_arguments()
        
        # Load results based on input source
        if args.input_source == 'json':
            print(f"Loading extraction results from JSON file: {args.input_file}")
            results = load_extraction_results(args.input_file)
        else:  # pddl_logs
            print(f"Loading PDDL results from logs directory: {args.logs_dir}")
            results = load_pddl_results_from_logs(args.logs_dir)
        
        if not results:
            print("No results found!")
            return
        
        # Create output directory
        output_path = Path(args.output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Initialize translator with OpenAI API
        translator = MimicFormatTranslator(
            model=args.model
        )
        
        # Process results for mimic translation
        processed_results = process_results_for_plan_to_code(
            results=results,
            translator=translator,
            output_dir=args.output_dir,
            batch_size=args.batch_size,
            validate_code=args.validate_code
        )
        
        # Save individual mimic files
        save_individual_plan_to_code_files(processed_results, args.output_dir)
        
        # Generate summary
        generate_summary(processed_results, args.output_dir)
        
    except Exception as e:
        print(f"Error in main execution: {str(e)}")
        print(f"Full error: {str(e.__class__.__name__)}: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main() 