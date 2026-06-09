(define (domain robot15)
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
    toaster - object
    fridge - object
    bread - object
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
