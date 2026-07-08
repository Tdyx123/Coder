# EXAMPLE 1 - Task Description: Turn off the light, turn on the faucet and leave the house. 
#GENERAL TASK DECOMPOSITION
#Decompose and parallelize subtasks where ever possible
#Independent subtasks:
#SubTask 1: Turn off the Light. (Skills Required: GoToObject, SwitchOff)
#SubTask 2: Turn on the Faucet. (Skills Required: GoToObject, SwitchOn)
#SubTask 3: Leave the House. (Skills Required: GoToObject, OpenObject,CloseObject)
#We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. SubTask 3 is dependent on SubTask 1 and 2 being completed.

#action description from domain for tasks required
#Subtask 1: Turn off the Light

# Initial Precondition analyze due to previous subtask:
#1. Robot not at light switch location

GoToObject: Robot goes to the light switch.
Parameters: ?robot , ?light_switch
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?light_switch), (not (inaction ?robot))

SwitchOff: Robot turns off the light.
Parameters: ?robot, ?light_switch
Preconditions: (at ?robot ?light_switch), (not (inaction ?robot)), (switch-on ?light_switch)
Effects: (not (switch-on ?light_switch)), (not (inaction ?robot))

#Subtask 2: Turn on the Faucet

# Initial Precondition analyze due to previous subtask:
#1. Robot not at Faucet location

GoToObject: Robot goes to the faucet.
Parameters: ?robot , ?faucet
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?faucet), (not (inaction ?robot))

SwitchOn: Robot turns off the faucet.
Parameters: ?robot, ?faucet
Preconditions: (at ?robot ?faucet), (not (inaction ?robot)), (not (switch-on ?faucet))
Effects: (switch-on ?faucet), (not (inaction ?robot))

#Subtask 3: Leave the House

# Initial Precondition analyze due to previous subtask:
#1. Robot not at the door

GoToObject: Robot goes to the door.
Parameters: ?robot, ?door
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?door), (not (inaction ?robot))

OpenObject: Robot opens the door.
Parameters: ?robot, ?door
Preconditions: (not (inaction ?robot)), (at ?robot ?door), (not(object-open ?door))
Effects: (object-open ?door), (not (inaction ?robot))

CloseObject: Robot closes the door.
Parameters: ?robot, ?door
Preconditions: (not (inaction ?robot)), (at ?robot ?door), (object-open ?door)
Effects: (not(object-open ?door)), (not (inaction ?robot))

# TASK ALLOCATION
# Scenario: There are 2 robots available. Use available robots to execute independent subtasks in parallel whenever dependencies and robot capabilities allow. Robots should be assigned to subtasks that match their skills and mass capacity. 
# Task Allocation Rules: Each subtask should be assigned to a robot who has all skills required for the subtask and can handle the mass of the objects involved in the task. If a subtask can be performed in parallel with other subtasks (i.e., it does not depend on the completion of other tasks), assign it to a robot or team  that can start immediately and can handle the mass of the objects involved. If a subtask must be performed sequentially after other subtasks (i.e., it depends on the completion of other subtasks), assign it to a robot or team that can start as soon as the preceding subtasks are complete and can handle the mass of the objects involved. Based on the 'GENERAL TASK DECOMPOSITION' given above, identify the subtasks for each main task, the skills required for each subtask, and the mass of the objects and mass capacity of robots involved in each subtask. Determine whether the subtasks must be performed sequentially or in parallel, or a combination of both, and whether the mass of the objects involved in the subtasks is within the mass capacity of the robots or teams.
robots = [{'name': 'robot1', 'no_skills': 9, 'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'SwitchOff', 'SliceObject', 'DropHandObject', 'ThrowObject', 'PushObject', 'PullObject'],'mass_capacity': 2}, {'name': 'robot2', 'no_skills': 13, 'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'PickupObject', 'PutObject', 'SwitchOn', 'SwitchOff', 'DropHandObject', 'ThrowObject', 'PushObject', 'PullObject'],'mass_capacity': 2}]
objects = [{'name': 'SaltShaker', 'mass': 1.0}, {'name': 'SoapBottle', 'mass': 5.0}, {'name': 'Bowl', 'mass': 3.0}, {'name': 'CounterTop', 'mass': 2}, {'name': 'Drawer', 'mass': 5}, {'name': 'Vase', 'mass': 1}, {'name': 'Lettuce', 'mass': 5}, {'name': 'Plate', 'mass': 5}, {'name': 'Potato', 'mass': 2}, {'name': 'Pan', 'mass': 5}, {'name': 'Window', 'mass': 4}, {'name': 'Fridge', 'mass': 4}, {'name': 'Sink', 'mass': 5}, {'name': 'Stool', 'mass': 6}, {'name': 'DiningTable', 'mass': 6}, {'name': 'HousePlant', 'mass': 2}, {'name': 'Chair', 'mass': 6}, {'name': 'Cup', 'mass': 3}, {'name': 'Shelf', 'mass': 6}, {'name': 'StoveKnob', 'mass': 3}, {'name': 'ButterKnife', 'mass': 6}, {'name': 'Bread', 'mass': 2}, {'name': 'DishSponge', 'mass': 4}, {'name': 'Floor', 'mass': 4}, {'name': 'PepperShaker', 'mass': 2}, {'name': 'Spatula', 'mass': 2}, {'name': 'StoveBurner', 'mass': 4}, {'name': 'Statue', 'mass': 1}, {'name': 'Kettle', 'mass': 2}, {'name': 'Apple', 'mass': 4}, {'name': 'WineBottle', 'mass': 5}, {'name': 'Knife', 'mass': 6}, {'name': 'GarbageCan', 'mass': 5}, {'name': 'Microwave', 'mass': 5}, {'name': 'LightSwitch', 'mass': 6}, {'name': 'Pot', 'mass': 2}, {'name': 'Egg', 'mass': 3}, {'name': 'Mug', 'mass': 2}, {'name': 'CoffeeMachine', 'mass': 6}, {'name': 'Faucet', 'mass': 6}, {'name': 'Tomato', 'mass': 4}, {'name': 'Spoon', 'mass': 2}, {'name': 'Fork', 'mass': 4.8}, {'name': 'Toaster', 'mass': 1}, {'name': 'Book', 'mass': 6}, {'name': 'SinkBasin', 'mass': 6}, {'name': 'Cabinet', 'mass': 1}, {'name': 'ShelvingUnit', 'mass': 3}]
# IMPORTANT: The AI should ensure that the robots assigned to the tasks have all the necessary skills to perform the tasks. IMPORTANT: Determine whether the subtasks must be performed sequentially or in parallel, or a combination of both and allocate robots based on availablitiy. "

# SOLUTION
# Robot 1 has 9 skills, while Robot 2 has 13 skills. Robots do not have same number of skills. 
# All the robots DONOT share the same set and number of skills (no_skills) and objects have different mass. In this case where all robots have different sets of skills and objects have different mass - Focus on Task Allocation based on Robot Skills alone. 
# Analyze the skills required for each subtask and the skills each robot possesses. In this scenario, we have three subtaskes: 'Turn off the light', 'Turn on the faucet' and 'Leave the house'.
# For the 'Turn off the light' subtask, it can be performed by any robot with 'GoToObject' and 'SwitchOff' skills. In this case, Robots 1 has all these skills.
# For the 'Turn on the faucet' subtask, it can be performed by any robot with 'GoToObject' and 'SwitchOn' skills. In this case, Robots 2 has all these skills.
# For the 'Leave the house' subtask, it can performed only after subtask 1 and subtask 2 is done. And it can be done by any robot with 'GoToObject', 'OpenObject' and 'CloseObject' skills. In this case, Robots 1 can do this subtask.
# 'Turn off the light' subtask and 'Turn on the faucet' subtask can be performed in parallel. 

# Sequence of Operations:
Subtask 1: Robot 1;Subtask 2: Robot 2;
Subtask 3: Robot 2;

# EXAMPLE 2 - Task Description: Slice the Potato 
#GENERAL TASK DECOMPOSITION
#Decompose and parallelize subtasks where ever possible
#SubTask: Slice the Potato (Skills Required: GoToObject, PickupObject, SliceObject)
#We can sequential run subtask #1 because Subtask 1 is the only task.

#action description from domain for tasks required

#Subtask 1: Slice the Potato

# Initial Precondition analyze due to previous subtask:
#1. Robot not at the potato
#2. Robot not holding knife

GoToObject: Robot goes to the knife
Parameters: ?robot, ?knife
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?knife), (not (inaction ?robot))

PickupObject: Robot pickes up the knife
Parameters: ?robot, ?knife, ?knife_location 
Preconditions: (at-location ?knife ?knife_location), (at ?robot ?knife), (not (inaction ?robot))
Effects: (holding ?robot ?knife), (not (inaction ?robot))

GoToObject: Robot goes to the potato
Parameters: ?robot, ?potato
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?potato), (not (inaction ?robot))

SliceObject: Robot slices the potato
Parameters: ?robot, ?potato
Preconditions: (holding ?robot ?knife), (not (inaction ?robot))
Effects: (sliced ?potato), (not (inaction ?robot))

#Task Slice the Potato is done.


# TASK ALLOCATION
# Scenario: There are 3 robots available. Use available robots to execute independent subtasks in parallel whenever dependencies and robot capabilities allow. Robots should be assigned to subtasks that match their skills and mass capacity. 
# Task Allocation Rules: Each subtask should be assigned to a robot who has all skills required for the subtask and can handle the mass of the objects involved in the task. If a subtask can be performed in parallel with other subtasks (i.e., it does not depend on the completion of other tasks), assign it to a robot or team  that can start immediately and can handle the mass of the objects involved. If a subtask must be performed sequentially after other subtasks (i.e., it depends on the completion of other subtasks), assign it to a robot or team that can start as soon as the preceding subtasks are complete and can handle the mass of the objects involved. Based on the 'GENERAL TASK DECOMPOSITION' given above, identify the subtasks for each main task, the skills required for each subtask, and the mass of the objects and mass capacity of robots involved in each subtask. Determine whether the subtasks must be performed sequentially or in parallel, or a combination of both, and whether the mass of the objects involved in the subtasks is within the mass capacity of the robots or teams.
robots = [{'name': 'robot1', 'no_skills': 5, 'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'PickupObject', 'SliceObject'],'mass_capacity': 2}, {'name': 'robot2', 'no_skills': 10, 'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SwitchOn', 'SwitchOff', 'DropHandObject', 'ThrowObject', 'PushObject', 'PullObject'],'mass': 2}, {'name': 'robot3', 'no_skills': 7, 'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'PickupObject', 'PutObject', 'DropHandObject'],'mass_capacity': 2}]
objects = [{'name': 'SaltShaker', 'mass':1.0}, {'name': 'SoapBottle', 'mass': 5.0}, {'name': 'Bowl', 'mass': 3.0}, {'name': 'CounterTop', 'mass': 2}, {'name': 'Drawer', 'mass': 5}, {'name': 'Vase', 'mass': 1}, {'name': 'Lettuce', 'mass': 5}, {'name': 'Plate', 'mass': 5}, {'name': 'Potato', 'mass': 2}, {'name': 'Pan', 'mass': 5}, {'name': 'Window', 'mass': 4}, {'name': 'Fridge', 'mass': 4}, {'name': 'Sink', 'mass': 5}, {'name': 'Stool', 'mass': 6}, {'name': 'DiningTable', 'mass': 6}, {'name': 'HousePlant', 'mass': 2}, {'name': 'Chair', 'mass': 6}, {'name': 'Cup', 'mass': 3}, {'name': 'Shelf', 'mass': 6}, {'name': 'StoveKnob', 'mass': 3}, {'name': 'ButterKnife', 'mass': 6}, {'name': 'Bread', 'mass': 2}, {'name': 'DishSponge', 'mass': 4}, {'name': 'Floor', 'mass': 4}, {'name': 'PepperShaker', 'mass': 2}, {'name': 'Spatula', 'mass': 2}, {'name': 'StoveBurner', 'mass': 4}, {'name': 'Statue', 'mass': 1}, {'name': 'Kettle', 'mass': 2}, {'name': 'Apple', 'mass': 4}, {'name': 'WineBottle', 'mass': 5}, {'name': 'Knife', 'mass': 6}, {'name': 'GarbageCan', 'mass': 5}, {'name': 'Microwave', 'mass': 5}, {'name': 'LightSwitch', 'mass': 6}, {'name': 'Pot', 'mass': 2}, {'name': 'Egg', 'mass': 3}, {'name': 'Mug', 'mass': 2}, {'name': 'CoffeeMachine', 'mass': 6}, {'name': 'Faucet', 'mass': 6}, {'name': 'Tomato', 'mass': 4}, {'name': 'Spoon', 'mass': 2}, {'name': 'Fork', 'mass': 4.8}, {'name': 'Toaster', 'mass': 1}, {'name': 'Book', 'mass': 6}, {'name': 'SinkBasin', 'mass': 6}, {'name': 'Cabinet', 'mass': 1}, {'name': 'ShelvingUnit', 'mass': 3}]
# IMPORTANT: The AI should ensure that the robots assigned to the tasks have all the necessary skills to perform the tasks. IMPORTANT: Determine whether the subtasks must be performed sequentially or in parallel, or a combination of both and allocate robots based on availablitiy. "

# SOLUTION
# Robot 1 has 5 skills, while Robot 2 has 10 and robot 3 has 7 skills. Robots do not have same number of skills.
# All the robots DONOT share the same set and number of skills (no_skills) and objects have different mass. In this case where all robots have different sets of skills and objects have different mass - Focus on Task Allocation based on Robot Skills alone. 
# Analyze the skills required for each subtask and the skills each robot possesses. In this scenario, we have one main subtasks: 'Slice the Potato'.
# For the 'Slice the Potato' subtask, it can be performed by any robot with 'GoToObject', 'PickupObject' and 'SliceObject' skills. In this case, Robot 1 has all these skills.

# Sequence of Operations:
Subtask 1: Robot 1;
