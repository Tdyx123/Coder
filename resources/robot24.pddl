(define (domain robot24)
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
    microwave - object
    toaster - object
    stove_burner - object
    sink - object
    candle - object
    bread - object
  )

  (:predicates
    (at ?robot - robot ?object - object)
    (holding ?robot - robot ?object - object)
    (at-location ?object - object ?location - object)
    (switch-on ?object - object)
    (sliced ?object - object)
    (hot ?object - object)
    (filled-with-water ?object - object)
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

  (:action RunMicrowave
    :parameters (?r - robot ?m - microwave ?item - object)
    :precondition (and
      (at ?r ?m)
      (at-location ?item ?m)
      (not (object-open ?m))
    )
    :effect (and
      (hot ?item)
      (when (cookable-by-microwave ?item)
        (cooked ?item)
      )
    )
  )

  (:action RunToaster
    :parameters (?r - robot ?t - toaster ?b - bread)
    :precondition (and
        (at ?r ?t)
        (at-location ?b ?t)
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
      (not (holding ?r ?object))
      (at-location ?object ?sb)
    )
  )

  (:action FireByStoveBurner
    :parameters (?r - robot ?sb - stove_burner ?candle - candle)
    :precondition (and
        (at ?r ?sb)
        (holding ?r ?candle)
      )
    :effect (and
        (switch-on ?candle)
      )
  )

  (:action FillWater
    :parameters (?r - robot ?sink - sink ?object - object)
    :precondition (and
        (at ?r ?sink)
        (holding ?r ?object)
      )
    :effect (and
        (filled-with-water ?object)
      )
  )
)
