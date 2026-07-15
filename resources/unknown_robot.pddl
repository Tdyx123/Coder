(define (domain allactionrobot)
  (:requirements :strips :typing :negative-preconditions) 
  (:types robot object)
  (:predicates
    (at ?robot - robot ?object - object)
    (holding ?robot - robot ?object - object)
    (at-location  ?object - object ?location - object)
    (switch-on ?robot - robot ?object - object)
    (switch-off ?robot - robot ?object - object)
    (object-open ?robot - robot ?object - object)
    (object-close ?robot - robot ?object - object)
    (break ?robot - robot ?object - object)
    (sliced ?object - object)
    (cleaned ?robot - robot ?object - object)
  )
  
  (:action GoToObject
    :parameters (?robot - robot ?object - object)
    :effect (and 
                  (at ?robot ?object)
    )
  )

  (:action PickupObject
    :parameters (?robot - robot ?object - object ?location - object)
    :precondition (and 
                    (at-location ?object ?location)
                    (at ?robot ?location)
    )
    :effect (and
              (holding ?robot ?object)
    )
  )

  (:action PutObject
    :parameters (?robot - robot ?object  - object ?location - object)
    :precondition (and 
                    (holding ?robot ?object)
                    (at ?robot ?location)
    )
    :effect (and
              (at-location ?object ?location)
              (not (holding ?robot ?object))
    )
  )
  
  (:action SwitchOn
    :parameters (?robot - robot ?object - object)
    :precondition (and 
                    (at ?robot ?object)
    )   
    :effect (and
              (switch-on ?robot ?object)
    ) 
  )


  (:action Switchoff
    :parameters (?robot - robot ?object - object)
    :precondition (and
                    (at ?robot ?object)
    )
    :effect (and
                (switch-off ?robot ?object)
    )    
  )


  (:action OpenObject
    :parameters (?robot - robot ?object - object)
    :precondition (and
                    (at ?robot ?object)
    )
      
    :effect (and
                (object-open ?robot ?object)
    )
  )


  (:action BreakObject
    :parameters (?robot - robot ?object - object)
    :precondition (and
                    (at ?robot ?object)
    )
    :effect (and
              (break ?robot ?object)
    )
  )
 

  (:action CloseObject
    :parameters (?robot - robot ?object - object)
    :precondition (and
                    (at ?robot ?object)
    )
    :effect (and
              (object-close ?robot ?object)
  )
  )


  (:action SliceObject
    :parameters (?robot - robot ?object - object)
    :precondition (and
                    (at ?robot ?object)
    )
    :effect (and
              (sliced ?object)
    )
  )    

 (:action CleanObject
    :parameters (?robot - robot ?object - object)
    :precondition (and
                    (at ?robot ?object)
    )
    :effect (and
              (cleaned ?robot ?object)
    )    
  )
)
