(define (domain robot27)
  (:requirements
    :strips
    :typing
    :negative-preconditions
    :conditional-effects
    :universal-preconditions
    :disjunctive-preconditions
  )

  (:types
    robot
    object
    toaster - object
    coffee_machine - object
    fridge - object
    mug - object
    bread - object
  )

  (:predicates
    (at ?robot - robot ?object - object)
    (holding ?robot - robot ?object - object)
    (at-location ?object - object ?location - object)
    (broken ?object - object)
    (sliced ?object - object)
    (hot ?object - object)
    (cold ?object - object)
    (filled-with-water ?object - object)
    (filled-with-coffee ?object - object)
    (cooked ?object - object)
  )

  (:action GoToObject
    :parameters (?r - robot ?o - object)
    :effect (and
        (forall
          (?x - object)
          (when
            (at ?r ?x)
            (not
              (at ?r ?x)
            )
          )
        )
        (at ?r ?o)
      )
  )

  (:action BreakObject
    :parameters (?r - robot ?o - object)
    :precondition (and
        (at ?r ?o)
        (not
          (broken ?o)
        )
      )
    :effect (and
        (broken ?o)
      )
  )

  (:action RunCoffeeMachine
    :parameters (?r - robot ?cm - coffee_machine ?m - mug)
    :precondition (and
        (at ?r ?cm)
        (at-location ?m ?cm)
        (not
          (filled-with-water ?m)
        )
        (not
          (filled-with-coffee ?m)
        )
      )
    :effect (and
        (filled-with-coffee ?m)
      )
  )

  (:action RunToaster
    :parameters (?r - robot ?t - toaster ?b - bread)
    :precondition (and
        (at ?r ?t)
        (holding ?r ?b)
        (sliced ?b)
      )
    :effect (and
        (hot ?b)
        (cooked ?b)
      )
  )

  (:action ColdObject
    :parameters (?r - robot ?fridge - fridge ?object - object)
    :precondition (and
      (at ?r ?fridge)
      (at-location ?object ?fridge)
      (not (object-open ?fridge))
      (switch-on ?fridge)
    )
    :effect (and
        (cold ?object)
      )
  )
)
