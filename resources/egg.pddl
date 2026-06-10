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
    (at-location ?object - object ?location - object)
    (is-openable ?object - object)
    (object-open ?object - object)
    (broken ?object - object)
    (cookable-by-stove_burner ?object - object)
    (cookable-by-microwave ?object - object)
    (placable_on_stove_burner ?object - object)
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
)
