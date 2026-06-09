(define (domain robot19)
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
    microwave - object
    sink - object
  )

  (:predicates
    (at ?robot - robot ?object - object)
    (holding ?robot - robot ?object - object)
    (at-location ?object - object ?location - object)
    (broken ?object - object)
    (sliced ?object - object)
    (is-openable ?object - object)
    (hot ?object - object)
    (object-open ?object - object)
    (filled-with-water ?object - object)
    (cookable-by-stove_burner ?object - object)
    (cookable-by-microwave ?object - object)
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
