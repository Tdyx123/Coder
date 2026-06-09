(define (domain robot7)
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
    toaster - object
    coffee_machine - object
    fridge - object
    stove_burner - object
    mug - object
    bread - object
    egg - object
  )

  (:predicates
    (at ?robot - robot ?object - object)
    (holding ?robot - robot ?object - object)
    (at-location ?object - object ?location - object)
    (switch-on ?object - object)
    (broken ?object - object)
    (sliced ?object - object)
    (is-openable ?object - object)
    (hot ?object - object)
    (cold ?object - object)
    (object-open ?object - object)
    (filled-with-water ?object - object)
    (filled-with-coffee ?object - object)
    (cookable-by-stove_burner ?object - object)
    (cookable-by-microwave ?object - object)
    (cooked ?object - object)
    (placable_on_stove_burner ?object - object)
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

  (:action PickupObject
    :parameters (?r - robot ?o - object ?loc - object)
    :precondition (and
        (at-location ?o ?loc)
        (or
          (at ?r ?o)
          (at ?r ?loc)
        )
        (or
          (not
            (is-openable ?loc)
          )
          (object-open ?loc)
        )
      )
    :effect (and
        (holding ?r ?o)
        (forall
          (?x - object)
          (when
            (at-location ?o ?x)
            (not
              (at-location ?o ?x)
            )
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
          (not
            (is-openable ?loc)
          )
          (object-open ?loc)
        )
      )
    :effect (and
        (at-location ?o ?loc)
        (not
          (holding ?r ?o)
        )
      )
  )

  (:action SwitchOn
    :parameters (?r - robot ?o - object)
    :precondition (and
        (at ?r ?o)
        (not
          (switch-on ?o)
        )
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
        (not
          (switch-on ?o)
        )
      )
  )

  (:action OpenObject
    :parameters (?r - robot ?o - object)
    :precondition (and
        (is-openable ?o)
        (at ?r ?o)
        (not
          (object-open ?o)
        )
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
        (not
          (object-open ?o)
        )
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
      (cookable-by-microwave ?egg)
    )
  )

  (:action SliceObject
    :parameters (?r - robot ?o - object ?loc - object ?k - knife)
    :precondition (and
        (not
          (sliced ?o)
        )
        (at-location ?o ?loc)
        (at ?r ?loc)
        (holding ?r ?k)
      )
    :effect (and
        (sliced ?o)
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

  (:action CookByStoveBurner
    :parameters (?r - robot ?sb - stove_burner ?container - object ?food - object)
    :precondition (and
      (at ?r ?sb)
      (placable_on_stove_burner ?container)
      (at-location ?food ?container)
      (holding ?r ?container)
      (cookable-by-stove_burner ?food)
    )
    :effect (and
      (cooked ?food)
    )
  )

  (:action HeatByStoveBurner
    :parameters (?r - robot ?sb - stove_burner ?object - object)
    :precondition (and
        (at ?r ?sb)
        (holding ?r ?object)
        (placable_on_stove_burner ?object)
      )
    :effect (and
      (hot ?object)
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
