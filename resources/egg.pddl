(define (domain egg)
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

    egg - object
  )

  (:predicates
    (at ?robot - robot ?object - object)
    (broken ?object - object)
    (cookable-by-stove_burner ?object - object)
    (cookable-by-microwave ?object - object)
  )

  (:action BreakEgg
    :parameters (?r - robot ?egg - egg)
    :precondition (and
      (at ?r ?egg)
      (not (broken ?egg))
    )
    :effect (and
      (broken ?egg)
      (cookable-by-stove_burner ?egg)
      (cookable-by-microwave ?egg)
    )
  )
)
