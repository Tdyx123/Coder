(define (domain allactionrobot)
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


  (:action PutObject
    :parameters (?robot - robot ?object - object ?location - object)
    :precondition (and 
                    (holding ?robot ?object)
                    (not(inaction ?robot))
                    (at ?robot ?location)
                    (or (not (is-openable ?location))  ;; Check if location is not openable
                        (object-open ?location)))  ;; If it is openable, it must be open
    :effect (and
              (at-location ?object ?location)
              (not (holding ?robot ?object))
              (not (inaction ?robot))
    )
  )


  (:action SwitchOn
    :parameters (?robot - robot ?object - object)
    :precondition (and 
                    (not(inaction ?robot))
                    (at ?robot ?object)
                    (not(switch-on ?object))
    )   
    :effect (and
              (not(inaction ?robot))
              (switch-on ?object)
    ) 
  )


  (:action Switchoff
    :parameters (?robot - robot ?object - object)
    :precondition (and
                    (not(inaction ?robot))
                    (at ?robot ?object)
                    (switch-on ?object)
    )
    :effect (and
                (not(inaction ?robot))
                (not(switch-on ?object))
    )    
  )


  (:action OpenObject
    :parameters (?robot - robot ?object - object)
    :precondition (and
                    (not(inaction ?robot))
                    (at ?robot ?object)
                    (is-openable ?object)
                    (not(object-open ?object))
    )
      
    :effect (and
                (not(inaction ?robot))
                (object-open ?object)
    )
  )


  (:action BreakObject
    :parameters (?robot - robot ?object - object)
    :precondition (and
                    (not(inaction ?robot))
                    (at ?robot ?object)
    )
    :effect (and
              (not(inaction ?robot))
              (broken ?object)
    )
  )

  (:action CloseObject
    :parameters (?robot - robot ?object - object)
    :precondition (and
                    (not(inaction ?robot))
                    (at ?robot ?object)
                    (object-open ?object)
    )
    :effect (and
              (not(inaction ?robot))
              (not(object-open ?object))
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
  

  (:action CleanObject
    :parameters (?robot - robot ?object - object ?sink - sink)
    :precondition (and
                    (not (inaction ?robot))
                    (holding ?robot ?object)
                    (at ?robot ?sink)
    )
    :effect (and
              (not (inaction ?robot))
              (cleaned ?object)
    )    
  )


  (:action RunMicrowave
    :parameters (?robot - robot ?microwave - microwave ?item - object)
    :precondition (and
                    (not (inaction ?robot))
                    (at ?robot ?microwave)
                    (at-location ?item ?microwave)
                    (not(object-open ?microwave))
    )
    :effect (and
              (not (inaction ?robot))
              (heated ?item)
    )
  )

  (:action RunCoffeeMachine
    :parameters (?robot - robot ?coffee_machine - coffee_machine ?mug - mug)
    :precondition (and
                    (at ?robot ?coffee_machine)
                    (holding ?robot ?mug)
                    (not (inaction ?robot)))

    :effect (and
              (not (inaction ?robot))
              (containing-coffee ?mug)
    )
  )

  (:action RunToaster
    :parameters (?robot - robot ?toaster - toaster ?bread - bread)
    :precondition (and
                    (at ?robot ?toaster)
                    (holding ?robot ?bread)
                    (not (inaction ?robot))
    )
    :effect (and
              (not (inaction ?robot))
              (heated ?bread)
    )
  )
)


