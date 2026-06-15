# Task Description: Put an Egg in the Fridge, and place a pot containing Apple slices into the refrigerator.

# GENERAL TASK DECOMPOSITION 
# Decompose and parallel subtasks where ever possible
# For each subtask, the robot's skills meet the assigned subtask's requirements.
# Specifically, if a subtask involves picking up an object, the robot's mass_capacity must be strictly greater than the object's mass. 
# Independent subtasks:
# SubTask 1: Put an Egg in the Fridge. (Skills Required: GoToObject, PickupObject, OpenObject, PutObject, CloseObject)
# SubTask 2: Prepare Apple Slices. (Skills Required: GoToObject, PickupObject, SliceObject, PutObject)
# SubTask 3: Place the Pot with Apple Slices in the Fridge. (Skills Required: GoToObject, PickupObject, PutObject, OpenObject, CloseObject)
# We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other.

# action description from domain for tasks required

#Subtask 1 Put an Egg in the Fridge

# Initial condition analyze due to previous subtask:
#1. Robot not at egg location
#2. Robot not holding egg
#3. Fridge initally closed

GoToObject: Robot goes to the egg.
Parameters: ?robot , ?egg
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?egg), (not (inaction ?robot))

PickupObject: Robot picks up the egg.
Parameters: ?robot , ?egg , ?egg_location 
Preconditions: (at-location ?egg ?egg_location), (at ?robot ?egg), (not (inaction ?robot))
Effects: (holding ?robot ?egg), (not (inaction ?robot))

GoToObject: Robot goes to the fridge.
Parameters: ?robot , ?fridge  ,
Preconditions: (not (inaction ?robot))
effects: (at ?robot ?fridge), (not (inaction ?robot))

OpenObject: Robot opens the fridge.
Parameters: ?robot, ?fridge
Preconditions: (not (inaction ?robot)), (at ?robot ?fridge)
Effects: (object-open ?fridge), (not (inaction ?robot))

PutObject: Robot puts the egg inside the fridge.
Parameters: ?robot, ?egg, ?fridge
Preconditions: (holding ?robot ?egg), (at ?robot ?fridge), (not (inaction ?robot))
Effects: (at-location ?egg ?fridge), (not (holding ?robot ?egg)), (not (inaction ?robot))

CloseObject: Robot closes the fridge.
Parameters: ?robot, ?fridge 
Preconditions: (not (inaction ?robot)), (at ?robot ?fridge)
Effects: (not (object-open ?fridge)), (not (inaction ?robot))


#Subtask 2: Prepare Apple Slices
# Initial condition analyze due to previous subtask:
#1. Robot not holding knife
#2. Robot not at apple location

GoToObject: Robot goes to the knife
Parameters: ?robot, ?knife
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?knife), (not (inaction ?robot))

PickupObject: Robot pickes up the knife
Parameters: ?robot, ?knife, ?knife_location 
Preconditions: (at-location ?knife ?knife_location), (at ?robot ?knife), (not (inaction ?robot))
Effects: (holding ?robot ?knife), (not (inaction ?robot))

GoToObject: Robot goes to the apple
Parameters: ?robot, ?apple
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?apple), (not (inaction ?robot))

SliceObject: Robot slices the apple
Parameters: ?robot, ?apple
Preconditions: (holding ?robot ?knife), (not (inaction ?robot))
Effects: (sliced ?apple), (not (inaction ?robot))

PickupObject: Robot pickes up the apple
Parameters: ?robot, ?apple, ?apple_location 
Preconditions: (at-location ?apple ?apple_location), (at ?robot ?apple), (not (inaction ?robot))
Effects: (holding ?robot ?apple), (not (inaction ?robot))

GoToObject: Robot goes to the pot
Parameters: ?robot, ?pot
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?pot), (not (inaction ?robot))

PutObject: Robot puts the apple to the pot
Parameters: ?robot , ?apple, ?pot
Preconditions: (holding ?robot ?apple), (at ?robot ?pot), (not (inaction ?robot))
Effects: (at-location ?apple ?pot), (not (holding ?robot ?apple)), (not (inaction ?robot))


#subtask 3: Place the Pot with Apple Slices in the Fridge
# Inital condition analyze due to previous subtask:
#1. Robot at pot location
#2. Fridge is Fridge, and initally closed
#3. Robot not holding pot initally.

PickupObject: Robot pickes up the pot
Parameters: ?robot, ?pot, ?pot_location
Preconditions: (at-location ?pot ?pot_location), (at ?robot ?pot_location), (not (inaction ?robot))
Effects: (holding ?robot ?pot), (not (inaction ?robot))

GoToObject: Robot pickes up the fridge
Parameters: ?robot, ?fridge
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?fridge), (not (inaction ?robot))

OpenObject: Robot opens the fridge.
Parameters: ?robot, ?fridge
Preconditions: (not (inaction ?robot)), (at ?robot ?fridge)
Effects: (object-open ?fridge), (not (inaction ?robot))

PutObject: Robot puts the pot inside the fridge.
Parameters: ?robot, ?pot, ?fridge
Preconditions: (holding ?robot ?pot), (at ?robot ?fridge), (not (inaction ?robot))
Effects: (at-location ?pot ?fridge), (not (holding ?robot ?pot)), (not (inaction ?robot))

CloseObject: Robot closes the fridge.
Parameters: ?robot, ?fridge 
Preconditions: (not (inaction ?robot)), (at ?robot ?fridge)
Effects: (not (object-open ?fridge)), (not (inaction ?robot))

# Task Put an Egg in the Fridge, and place a pot containing Apple slices into the refrigerator is done.






# Task Description: Make a sandwich with sliced lettuce, sliced tomato, sliced bread and serve it on a washed plate.

# GENERAL TASK DECOMPOSITION
# Decompose and parallelize subtasks where ever possible
# Independent subtasks:
# SubTask 1: Slice the Lettuce. (Skills Required: GoToObject, PickupObject, SliceObject)
# SubTask 2: Slice the Tomato. (Skills Required: GoToObject, PickupObject, SliceObject)
# SubTask 3: Slice the Bread. (Skills Required: GoToObject, PickupObject, SliceObject)
# SubTask 4: Wash the Plate. (Skills Required: GoToObject, PickupObject, CleanObject)
# SubTask 5: Assemble Sandwich on Plate. (Skills Required: GoToObject, PickupObject, PutObject)
# We can parallelize SubTask 1 and SubTask 4 because they don't depend on each other.

# action description from domain for tasks required
#Subtask 1: Slice the Lettuce.

# Initial Precondition analyze due to previous subtask:
# 1. Robot not holding lettuce.
# 2. Robot not at lettuce location.

GoToObject: Robot goes to the knife
Parameters: ?robot, ?knife
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?knife), (not (inaction ?robot))

PickupObject: Robot pickes up the knife
Parameters: ?robot, ?knife, ?knife_location 
Preconditions: (at-location ?knife ?knife_location), (at ?robot ?knife), (not (inaction ?robot))
Effects: (holding ?robot ?knife), (not (inaction ?robot))

GoToObject: Robot goes to the lettuce
Parameters: ?robot, ?lettuce
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?lettuce), (not (inaction ?robot))

SliceObject: Robot slices the lettuce
Parameters: ?robot, ?lettuce
Preconditions: (holding ?robot ?knife), (not (inaction ?robot))
Effects: (sliced ?lettuce), (not (inaction ?robot))

#Subtask 2: Slice the Tomato.

# Initial Precondition analyze due to previous subtask:
#1. Robot not holding knife
#2. Robot not at tomate location

GoToObject: Robot goes to the knife
Parameters: ?robot, ?knife
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?knife), (not (inaction ?robot))

PickupObject: Robot pickes up the knife
Parameters: ?robot, ?knife, ?knife_location 
Preconditions: (at-location ?knife ?knife_location), (at ?robot ?knife), (not (inaction ?robot))
Effects: (holding ?robot ?knife), (not (inaction ?robot))

GoToObject: Robot goes to the tomato
Parameters: ?robot, ?tomato
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?tomato), (not (inaction ?robot))

SliceObject: Robot slices the tomato
Parameters: ?robot, ?tomato
Preconditions: (holding ?robot ?knife), (not (inaction ?robot))
Effects: (sliced ?tomato), (not (inaction ?robot))

#Subtask 3: Slice the Bread.

#1. Robot not holding bread
#2. Robot not at  bread location.


GoToObject: Robot goes to the knife
Parameters: ?robot, ?knife
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?knife), (not (inaction ?robot))

PickupObject: Robot pickes up the knife
Parameters: ?robot, ?knife, ?knife_location 
Preconditions: (at-location ?knife ?knife_location), (at ?robot ?knife), (not (inaction ?robot))
Effects: (holding ?robot ?knife), (not (inaction ?robot))

GoToObject: Robot goes to the bread
Parameters: ?robot, ?bread
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?bread), (not (inaction ?robot))

SliceObject: Robot slices the bread
Parameters: ?robot, ?bread
Preconditions: (holding ?robot ?knife), (not (inaction ?robot))
Effects: (sliced ?bread), (not (inaction ?robot))

#Subtask 4: Wash the Plate

#1. Robot not holding plate
#2. Robot not at plate

GoToObject: Robot goes to the plate
Parameters: ?robot, ?plate
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?plate), (not (inaction ?robot))

PickupObject: Robot pickes up the plate
Parameters: ?robot, ?plate, ?plate_location 
Preconditions: (at-location ?plate ?plate_location), (at ?robot ?plate), (not (inaction ?robot))
Effects: (holding ?robot ?plate), (not (inaction ?robot))
                                        
GoToObject: Robot goes to the sink
Parameters: ?robot, ?sink
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?sink), (not (inaction ?robot))

CleanObject: Robot cleans the plate.
Parameters: ?robot, ?plate, ?sink
Preconditions: (at ?robot ?sink), (holding ?robot ?plate), (not (inaction ?robot))
Effects: (cleaned ?robot ?plate), (not (inaction ?robot))

#Subtask 5: Assemble Sandwich on Plate

#1. Robot not holding bread, lettuce, or tomato
#2. Robot holding plate
#3. Robot not at bread, lettuce, or tomato location

GoToObject: Robot goes to the countertop
Parameters: ?robot, ?countertop
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?countertop), (not (inaction ?robot))

PutObject: Robot places the plate on the countertop.
Parameters: ?robot, ?plate, ?countertop
Preconditions: (holding ?robot ?plate), (at ?robot ?countertop), (not (inaction ?robot))
Effects: (at-location ?plate ?countertop), (not (holding ?robot ?plate)), (not (inaction ?robot))

GoToObject: Robot goes to the bread
Parameters: ?robot, ?bread
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?bread), (not (inaction ?robot))

PickupObject: Robot pickes up the bread
Parameters: ?robot, ?bread, ?bread_location 
Preconditions: (at-location ?bread ?bread_location), (at ?robot ?bread), (not (inaction ?robot))
Effects: (holding ?robot ?bread), (not (inaction ?robot))

GoToObject: Robot goes to the plate.
Parameters: ?robot, ?plate
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?plate), (not (inaction ?robot))

PutObject: Robot places the sliced bread on the plate.
Parameters: ?robot, ?bread, ?plate
Preconditions: (holding ?robot ?bread), (at ?robot ?plate), (not (inaction ?robot))
Effects: (at-location ?bread ?plate), (not (holding ?robot ?bread)), (not (inaction ?robot))

GoToObject: Robot goes to the sliced lettuce
Parameters: ?robot, ?lettuce
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?lettuce), (not (inaction ?robot))

PickupObject: Robot pickes up the sliced lettuce
Parameters: ?robot, ?lettuce, ?lettuce_location 
Preconditions: (at-location ?lettuce ?lettuce_location), (at ?robot ?lettuce), (not (inaction ?robot))
Effects: (holding ?robot ?lettuce), (not (inaction ?robot))

GoToObject: Robot goes to the plate.
Parameters: ?robot, ?plate
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?plate), (not (inaction ?robot))

PutObject: Robot places the sliced lettuce on the plate.
Parameters: ?robot, ?lettuce, ?plate
Preconditions: (holding ?robot ?lettuce), (at ?robot ?plate), (not (inaction ?robot))
Effects: (at-location ?lettuce ?plate), (not (holding ?robot ?lettuce)), (not (inaction ?robot))

GoToObject: Robot goes to the sliced tomato
Parameters: ?robot, ?tomato
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?tomato), (not (inaction ?robot))

PickupObject: Robot pickes up the tomato
Parameters: ?robot, ?tomato, ?tomato_location 
Preconditions: (at-location ?tomato ?tomato_location), (at ?robot ?tomato), (not (inaction ?robot))
Effects: (holding ?robot ?tomato), (not (inaction ?robot))

GoToObject: Robot goes to the plate.
Parameters: ?robot, ?plate
Preconditions: (not (inaction ?robot))
Effects: (at ?robot ?plate), (not (inaction ?robot))

PutObject: Robot places the sliced tomato on the plate.
Parameters: ?robot, ?tomato, ?plate
Preconditions: (holding ?robot ?tomato), (at ?robot ?plate), (not (inaction ?robot))
Effects: (at-location ?tomato ?plate), (not (holding ?robot ?tomato)), (not (inaction ?robot))

# Task Make a sandwich with sliced lettuce, sliced tomato, sliced bread and serve it on a washed plate is done.




