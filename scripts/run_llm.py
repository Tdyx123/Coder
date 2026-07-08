import copy
import glob
import json
import yaml
import os
import argparse
from pathlib import Path
from datetime import datetime
import random
import subprocess
import time
import re

from ai2thor_object_cache import get_ai2_thor_objects_cached

from llm_client import (
    complete_with_provider,
    extract_text,
    extract_response_metadata,
    extract_usage,
    get_provider_for_model,
    load_providers,
)
from llm_logger import log_llm_call

import sys
sys.path.append(".")

import resources.actions as actions
import resources.robots as robots

def get_client_for_model(model):
    provider = get_provider_for_model(model, get_providers())
    return provider, provider['name']

def LM(prompt, model, max_tokens=128, temperature=0, stop=None, logprobs=1, frequency_penalty=0):
    provider_config, provider = get_client_for_model(model)
    start_time = time.time()
    response = complete_with_provider(
        model=model,
        prompt=prompt,
        provider=provider_config,
        max_tokens=max_tokens,
        temperature=temperature,
        stop=stop,
        frequency_penalty=frequency_penalty,
    )
    duration_ms = (time.time() - start_time) * 1000
    text = extract_text(response)
    response_metadata = extract_response_metadata(response)
    usage = extract_usage(response)
    
    log_llm_call(
        model=model,
        provider=provider,
        messages=prompt if isinstance(prompt, list) else [{'role': 'user', 'content': prompt}],
        params={'max_tokens': max_tokens, 'temperature': temperature, 'frequency_penalty': frequency_penalty},
        response_text=text,
        usage=usage,
        duration_ms=duration_ms,
        key_index=response_metadata.get("key_index"),
    )
    
    return response, text

def get_providers():
    providers_file = Path(__file__).parent / 'providers.yaml'
    return load_providers(providers_file)

def get_models():
    return [model for provider in get_providers() for model in provider['models']]

def get_base_url(providers, model):
    for provider in providers:
        if model in provider['models']:
            return provider['base_url']
    return None

# Function returns object list with name and properties.
def convert_to_dict_objprop(objs, obj_mass):
    objs_dict = []
    for i, obj in enumerate(objs):
        obj_dict = {'name': obj , 'mass' : obj_mass[i]}
        # obj_dict = {'name': obj , 'mass' : 1.0}
        objs_dict.append(obj_dict)
    return objs_dict

def extract_floor_plan_number(floor_plan):
    floor_plan_str = str(floor_plan)
    if floor_plan_str.startswith("FloorPlan"):
        floor_plan_str = floor_plan_str[len("FloorPlan"):]

    match = re.match(r"(\d+)", floor_plan_str)
    if not match:
        raise ValueError(f"Invalid floor_plan value: {floor_plan}")

    return match.group(1)

def get_ai2_thor_objects(floor_plan_id):
    return get_ai2_thor_objects_cached(int(extract_floor_plan_number(floor_plan_id)), convert_to_dict_objprop)


def load_test_tasks(test_set, floor_plan):
    test_file = Path(f"./data/{test_set}/FloorPlan{floor_plan}.jsonl")
    test_tasks = []
    robots_test_tasks = []
    gt_test_tasks = []
    trans_cnt_tasks = []
    max_trans_cnt_tasks = []

    with open(test_file, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            record = json.loads(line)
            test_tasks.append(record["task"])
            robots_test_tasks.append(record["robot list"])
            gt_test_tasks.append(record["object_states"])
            trans_cnt_tasks.append(record["trans"])
            max_trans_cnt_tasks.append(record.get("max_trans", record.get("min_trans")))

    return test_tasks, robots_test_tasks, gt_test_tasks, trans_cnt_tasks, max_trans_cnt_tasks

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--floor-plan", type=str, required=True)
    parser.add_argument("--model", type=str, default="gpt-4o", 
                        choices = get_models())
    
    parser.add_argument("--prompt-decompse-set", type=str, default="train_task_decompose", 
                        choices=['train_task_decompose'])
    
    parser.add_argument("--prompt-allocation-set", type=str, default="train_task_allocation", 
                        choices=['train_task_allocation'])
    
    parser.add_argument("--test-set", type=str, default="final_test", 
                        choices=['final_test'])
    
    parser.add_argument("--log-results", type=bool, default=True)
    
    args = parser.parse_args()

    if not os.path.isdir(f"./logs/"):
        os.makedirs(f"./logs/")
        
    # read the tasks        
    test_tasks, robots_test_tasks, gt_test_tasks, trans_cnt_tasks, max_trans_cnt_tasks = \
        load_test_tasks(args.test_set, args.floor_plan)
                    
    print(f"\n----Test set tasks----\n{test_tasks}\nTotal: {len(test_tasks)} tasks\n")
    # prepare list of robots for the tasks
    available_robots = []
    for robots_list in robots_test_tasks:
        task_robots = []
        for i, r_id in enumerate(robots_list):
            rob = robots.robots [r_id-1]
            # rename the robot
            rob['name'] = 'robot' + str(i+1)
            task_robots.append(rob)
        available_robots.append(task_robots)
        
    
    ######## Train Task Decomposition ########
        
    # prepare train decompostion demonstration for ai2thor samples
    prompt = f"from skills import " + actions.ai2thor_actions
    prompt += f"\nimport time"
    prompt += f"\nimport threading"
    scene_floor_plan = int(extract_floor_plan_number(args.floor_plan))
    objects_ai = f"\n\nobjects = {get_ai2_thor_objects(scene_floor_plan)}"
    prompt += objects_ai
    
    # read input train prompts
    decompose_prompt_file = open(os.getcwd() + "/data/pythonic_plans/" + args.prompt_decompse_set + ".py", "r")
    decompose_prompt = decompose_prompt_file.read()
    decompose_prompt_file.close()
    
    prompt += "\n\n" + decompose_prompt
    
    print ("Generating Decompsed Plans...")
    
    decomposed_plan = []
    for task in test_tasks:
        curr_prompt =  f"{prompt}\n\n# Task Description: {task}"
                  
        messages = [{"role": "user", "content": curr_prompt}]
        _, text = LM(messages,args.model, max_tokens=1300, frequency_penalty=0.0)

        decomposed_plan.append(text)
        

    print ("Generating Allocation Solution...")


    #### task problem generation#####
    prompt = f"from skills import " + actions.ai2thor_actions
    prompt += f"\nimport time"
    prompt += f"\nimport threading"
    

    ######## Train Task Allocation - SOLUTION ########
    prompt = f"from skills import " + actions.ai2thor_actions
    prompt += f"\nimport time"
    prompt += f"\nimport threading"
    
    prompt_file = os.getcwd() + "/data/pythonic_plans/" + args.prompt_allocation_set + "_solution.py"
    allocated_prompt_file = open(prompt_file, "r")
    allocated_prompt = allocated_prompt_file.read()
    allocated_prompt_file.close()
    
    prompt += "\n\n" + allocated_prompt + "\n\n"
    
    allocated_plan = []
    for i, plan in enumerate(decomposed_plan):
        no_robot  = len(available_robots[i])
        curr_prompt = prompt + plan
        curr_prompt += f"\n# TASK ALLOCATION"
        curr_prompt += f"\n# Scenario: There are {no_robot} robots available. Use available robots to execute independent subtasks in parallel whenever dependencies and robot capabilities allow. Robots should be assigned to subtasks that match their skills and mass capacity. Using your reasoning come up with a solution to satisfy all constraints."
        curr_prompt += f"\n\nrobots = {available_robots[i]}"
        curr_prompt += f"\n{objects_ai}"
        curr_prompt += f"\n\n# IMPORTANT: The AI should ensure that the robots assigned to the tasks have all the necessary skills to perform the tasks. IMPORTANT: Determine whether the subtasks must be performed sequentially or in parallel, or a combination of both and allocate robots based on availablitiy. "
        curr_prompt += f"\n# SOLUTION  \n"

        messages = [{"role": "system", "content": "You are a Robot Task Allocation Expert. Determine whether the subtasks must be performed sequentially or in parallel, or a combination of both based on your reasoning. In the case of Task Allocation based on Robot Skills alone - First check if robot teams are required. Then Ensure that robot skills or robot team skills match the required skills for the subtask when allocating. Make sure that condition is met. In the case of Task Allocation based on Mass alone - First check if robot teams are required. Then Ensure that robot mass capacity or robot team combined mass capacity is greater than or equal to the mass for the object when allocating. Make sure that condition is met. In both the Task Task Allocation based on Mass alone and Task Allocation based on Skill alone, if there are multiple options for allocation, pick the best available option by reasoning to the best of your ability."},{"role": "system", "content": "You are a Robot Task Allocation Expert"},{"role": "user", "content": curr_prompt}]
        _, text = LM(messages, args.model, max_tokens=400, frequency_penalty=0.69)

        allocated_plan.append(text)
    
    print ("Generating Allocated Code...")
    
    ######## Train Task Allocation - CODE Solution ########

    prompt = f"from skills import " + actions.ai2thor_actions
    prompt += f"\nimport time"
    prompt += f"\nimport threading"
    prompt += objects_ai
    
    code_plan = []

    prompt_file1 = os.getcwd() + "/data/pythonic_plans/" + args.prompt_allocation_set + "_code.py"
    code_prompt_file = open(prompt_file1, "r")
    code_prompt = code_prompt_file.read()
    code_prompt_file.close()
    
    prompt += "\n\n" + code_prompt + "\n\n"

    for i, (plan, solution) in enumerate(zip(decomposed_plan,allocated_plan)):
        curr_prompt = prompt + plan
        curr_prompt += f"\n# TASK ALLOCATION"
        curr_prompt += f"\n\nrobots = {available_robots[i]}"
        curr_prompt += solution
        curr_prompt += f"\n# CODE Solution  \n"
                  
        messages = [{"role": "system", "content": "You are a Robot Task Allocation Expert"},{"role": "user", "content": curr_prompt}]
        _, text = LM(messages, args.model, max_tokens=1400, frequency_penalty=0.4)

        code_plan.append(text)
    
    # save generated plan
    exec_folders = []
    if args.log_results:
        line = {}
        now = datetime.now() # current date and time
        date_time = now.strftime("%m-%d-%Y-%H-%M-%S")
        
        for idx, task in enumerate(test_tasks):
            task_name = "{fxn}".format(fxn = '_'.join(task.split(' ')))
            task_name = task_name.replace('\n','')
            folder_name = f"{task_name}_plans_{date_time}"
            exec_folders.append(folder_name)
            
            os.mkdir("./logs/"+folder_name)
     
            with open(f"./logs/{folder_name}/log.txt", 'w') as f:
                f.write(task)
                f.write(f"\n\nModel: {args.model}")
                f.write(f"\n\nFloor Plan: {args.floor_plan}")
                f.write(f"\n{objects_ai}")
                f.write(f"\nrobots = {available_robots[idx]}")
                f.write(f"\nground_truth = {gt_test_tasks[idx]}")
                f.write(f"\ntrans = {trans_cnt_tasks[idx]}")
                f.write(f"\nmax_trans = {max_trans_cnt_tasks[idx]}")

            with open(f"./logs/{folder_name}/decomposed_plan.py", 'w') as d:
                d.write(decomposed_plan[idx])
                
            with open(f"./logs/{folder_name}/allocated_plan.py", 'w') as a:
                a.write(allocated_plan[idx])
                
            with open(f"./logs/{folder_name}/code_plan.py", 'w') as x:
                x.write(code_plan[idx])
