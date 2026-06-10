(define (domain coffee_machine)
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

    knife - object
    sink - object
    egg - object
    coffee_machine - object
    mug - object
  )

  (:predicates
    (at ?robot - robot ?object - object)
    (holding ?robot - robot ?object - object)
    (at-location ?object - object ?location - object)
    (switch-on ?object - object)
    (broken ?object - object)
    (sliced ?object - object)
    (cleaned ?object - object)
    (is-openable ?object - object)
    (object-open ?object - object)
    (filled-with-water ?object - object)
    (filled-with-coffee ?object - object)
    (cookable-by-stove_burner ?object - object)
    (cookable-by-microwave ?object - object)
    (placable_on_stove_burner ?object - object)
  )

  (:action GoToObject
    :parameters (?r - robot ?o - object)
    :effect (and
      (forall (?x - object)
        (when (at ?r ?x)
          (not (at ?r ?x))
        )
      )
      (at ?r ?o)
    )
  )

  (:action PickupObject
    :parameters (?r - robot ?o - object ?loc - object)
    :precondition (and
      (at-location ?o ?loc)
      (or
        (at ?r ?o)
        (at ?r ?loc)
      )
      (or
        (not (is-openable ?loc))
        (object-open ?loc)
      )
    )
    :effect (and
      (holding ?r ?o)
      (forall (?x - object)
        (when (at-location ?o ?x)
          (not (at-location ?o ?x))
        )
      )
    )
  )

  (:action PutObject
    :parameters (?r - robot ?o - object ?loc - object)
    :precondition (and
      (holding ?r ?o)
      (at ?r ?loc)
      (or
        (not (is-openable ?loc))
        (object-open ?loc)
      )
    )
    :effect (and
      (at-location ?o ?loc)
      (not (holding ?r ?o))
    )
  )

  (:action SwitchOn
    :parameters (?r - robot ?o - object)
    :precondition (and
      (at ?r ?o)
      (not (switch-on ?o))
    )
    :effect (and
      (switch-on ?o)
    )
  )

  (:action SwitchOff
    :parameters (?r - robot ?o - object)
    :precondition (and
      (at ?r ?o)
      (switch-on ?o)
    )
    :effect (and
      (not (switch-on ?o))
    )
  )

  (:action OpenObject
    :parameters (?r - robot ?o - object)
    :precondition (and
      (is-openable ?o)
      (at ?r ?o)
      (not (object-open ?o))
    )
    :effect (and
      (object-open ?o)
    )
  )

  (:action CloseObject
    :parameters (?r - robot ?o - object)
    :precondition (and
      (at ?r ?o)
      (object-open ?o)
    )
    :effect (and
      (not (object-open ?o))
    )
  )

  (:action BreakObject
    :parameters (?r - robot ?o - object)
    :precondition (and
      (at ?r ?o)
      (not (broken ?o))
    )
    :effect (and
      (broken ?o)
    )
  )

  (:action PrepareEgg
    :parameters (?r - robot ?egg - egg ?container - object)
    :precondition (and
      (at ?r ?egg)
      (at-location ?egg ?container)
      (not (broken ?egg))
      (placable_on_stove_burner ?container)
    )
    :effect (and
      (broken ?egg)
      (cookable-by-stove_burner ?egg)
    )
  )

  (:action SliceObject
    :parameters (?r - robot ?o - object ?loc - object ?k - knife)
    :precondition (and
      (not (sliced ?o))
      (at-location ?o ?loc)
      (at ?r ?loc)
      (holding ?r ?k)
    )
    :effect (and
      (sliced ?o)
    )
  )

  (:action CleanObject
    :parameters (?r - robot ?o - object ?s - sink)
    :precondition (and
      (holding ?r ?o)
      (at ?r ?s)
    )
    :effect (and
      (cleaned ?o)
    )
  )

  (:action RunCoffeeMachine
    :parameters (?r - robot ?cm - coffee_machine ?m - mug)
    :precondition (and
      (at ?r ?cm)
      (at-location ?m ?cm)
      (not (filled-with-water ?m))
      (not (filled-with-coffee ?m))
    )
    :effect (and
      (filled-with-coffee ?m)
    )
  )
)
