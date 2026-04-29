(define (domain robot27)
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
)
