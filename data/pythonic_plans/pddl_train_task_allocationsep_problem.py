#Example 1
#Subtask examination from action perspective:
# SubTask 1: Slice the Potato

# Initial Precondition analyze due to previous subtask:
#1. Robot not at the potato
#2. Robot not holding the potato

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

Assigned Robots: Robot 1
Objects Involved: potato, knife

#Domain file content,
(define (domain robot1)
  (:requirements :strips :typing :negative-preconditions :conditional-effects :universal-preconditions :disjunctive-preconditions)
  (:types 
    robot
    object
    knife sink microwave mug coffee_machine toaster bread - object)
  (:predicates
    (at ?robot - robot ?object - object)
    (inaction ?robot - robot) 
    (holding ?robot - robot ?object - object)
    (at-location  ?object - object ?location - object)
    (switch-on ?object - object)
    (broken ?object - object)
    (sliced ?object - object)
    (cleaned ?object - object)
    (is-openable ?object - object) 
    (heated ?object - object)
    (containing-coffee ?mug - mug)
    (object-open ?object - object)
  )

  (:action GoToObject
    :parameters (?robot - robot ?object - object)
    :precondition (not (inaction ?robot))

    :effect (and 
              (forall (?another_object - object)
                (when (at ?robot ?another_object)
                  (not (at ?robot ?another_object))
                )
              )
              (at ?robot ?object)
              (not (inaction ?robot))
            )
  )


  (:action PickupObject      
    :parameters (?robot - robot ?object - object ?location - object)
    :precondition (and 
                    (at-location ?object ?location)
                    (or (at ?robot ?object)
                      (at ?robot ?location))
                    (or (not(is-openable ?location))
                      (object-open ?location))
                    (not(inaction ?robot)))
    :effect (and
              (holding ?robot ?object)
              (not(at-location ?object ?location))
              (not(inaction ?robot))
    )
  )

  (:action SliceObject
    :parameters (?robot - robot ?object - object ?location - object ?knife - object)
    :precondition (and 
                    (at-location ?object ?location)
                    (at ?robot ?location)
                    (holding ?robot ?knife)
                    (not (inaction ?robot))
    )
    :effect (and
              (not (inaction ?robot))
              (sliced ?object)
    )
  )   

)

#based on the objects availiable for potential usage below."
objects = [{'name': 'SaltShaker', 'mass': 1.0}, {'name': 'SoapBottle', 'mass': 5.0}, {'name': 'Bowl', 'mass': 3.0}, {'name': 'CounterTop', 'mass': 2}, {'name': 'Drawer', 'mass': 5}, {'name': 'Vase', 'mass': 1}, {'name': 'Lettuce', 'mass': 5}, {'name': 'Plate', 'mass': 5}, {'name': 'Potato', 'mass': 2}, {'name': 'Pan', 'mass': 5}, {'name': 'Window', 'mass': 4}, {'name': 'Fridge', 'mass': 4}, {'name': 'Sink', 'mass': 5}, {'name': 'Stool', 'mass': 6}, {'name': 'DiningTable', 'mass': 6}, {'name': 'HousePlant', 'mass': 2}, {'name': 'Chair', 'mass': 6}, {'name': 'Cup', 'mass': 3}, {'name': 'Shelf', 'mass': 6}, {'name': 'StoveKnob', 'mass': 3}, {'name': 'ButterKnife', 'mass': 6}, {'name': 'Bread', 'mass': 2}, {'name': 'DishSponge', 'mass': 4}, {'name': 'Floor', 'mass': 4}, {'name': 'PepperShaker', 'mass': 2}, {'name': 'Spatula', 'mass': 2}, {'name': 'StoveBurner', 'mass': 4}, {'name': 'Statue', 'mass': 1}, {'name': 'Kettle', 'mass': 2}, {'name': 'Apple', 'mass': 4}, {'name': 'WineBottle', 'mass': 5}, {'name': 'Knife', 'mass': 6}, {'name': 'GarbageCan', 'mass': 5}, {'name': 'Microwave', 'mass': 5}, {'name': 'LightSwitch', 'mass': 6}, {'name': 'Pot', 'mass': 2}, {'name': 'Egg', 'mass': 3}, {'name': 'Mug', 'mass': 2}, {'name': 'CoffeeMachine', 'mass': 6}, {'name': 'Faucet', 'mass': 6}, {'name': 'Tomato', 'mass': 4}, {'name': 'Spoon', 'mass': 2}, {'name': 'Fork', 'mass': 4.8}, {'name': 'Toaster', 'mass': 1}, {'name': 'Book', 'mass': 6}, {'name': 'SinkBasin', 'mass': 6}, {'name': 'Cabinet', 'mass': 1}, {'name': 'ShelvingUnit', 'mass': 3}]

#Task description: generate the problem file. Step by step, based on the objects potential useful availiable above, the domain file precondition, actions and subtask examination. Note that the robot initate as not inaction is enough(the statement includes robot location)
#IMPORTANT, strictly follow the structure ,stop generate after the Problem file is generated.
#To generate the problem file for the given domain and task of slicing a potato, we need to follow these steps:
#Let's start by defining the objects and their initial states based on the provided list and task description.
#Step 1: Identify Objects and Initial State

#Objects Involved:

    Robots: robot1
    object: potato
    object: knife
    Inital location of potato,knife

#Initial States:


    at-location potato counterTop
    at-location knife counterTop
    not (inaction robot1)

#Step 2: Define Goals

Goals:

    sliced potato

pddl

(define (problem slice_potato_problem)
  (:domain robot1)
  (:objects
    robot1 - robot
    potato - object
    knife - object
    counterTop - object
    robot1_init_location - object
  )
  (:init
    (at robot1 robot1_init_location)
    (at-location potato counterTop)
    (at-location knife counterTop)
    not (inaction robot1)
  )
  (:goal
    (sliced potato)
  )
)

#Problem file generation is done
#IMPORTANT stop generating after seeing Problem file generation is done.