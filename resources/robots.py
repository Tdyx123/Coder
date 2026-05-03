# List of robots with different configurations 

# ALL SKILLS - INF MASS (robot1,robot2,robot3,robot4)
robot1 = {'name': 'robot1',   'no_skills': 10,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject', 'CleanObject'], 'mass_capacity' : 100}

robot2 = {'name': 'robot2',   'no_skills': 9,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 100}

robot3 = {'name': 'robot3',   'no_skills': 9,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 100}

robot4 = {'name': 'robot4',   'no_skills': 9,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 100}

# ALL SKILLS - Different MASS (robot5,robot6,robot7,robot8,robot9,robot10)
robot5 = {'name': 'robot5',   'no_skills': 9,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 1.0}

robot6 = {'name': 'robot6',   'no_skills': 9,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 2.1}

robot7 = {'name': 'robot7',   'no_skills': 9,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 0.08}

robot8 = {'name': 'robot8',   'no_skills': 9,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 0.4}

robot9 = {'name': 'robot9',   'no_skills': 11,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject', 'CleanObject', 'RunMicrowave'], 'mass_capacity' : 5}

robot10 = {'name': 'robot10',   'no_skills': 11,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject', 'CleanObject', 'RunMicrowave'], 'mass_capacity' : 0.02}

robot28 = {'name': 'robot28',   'no_skills': 9,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 2}

# Specific Skills for Robots (robot11,robot12,robot13,robot14,robot15,robot16,robot17)
# NO - OC  & OF
robot11 = {'name': 'robot11',  'no_skills': 5,   'skills': ['GoToObject', 'BreakObject', 'SliceObject', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 100}

# NO - OC  & PP
robot12 = {'name': 'robot12',  'no_skills': 5,   'skills': ['GoToObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff'], 'mass_capacity' : 100}

# NO - OC  & S
robot13 = {'name': 'robot13',  'no_skills': 7,   'skills': ['GoToObject', 'BreakObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject', 'CleanObject'], 'mass_capacity' : 100}

# NO - OC  & T
robot14 = {'name': 'robot14',  'no_skills': 7,   'skills': ['GoToObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 100}

# NO - OC  
robot15 = {'name': 'robot15',  'no_skills': 7,   'skills': ['GoToObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 100}

# ----------------------------------------------------------------------------------------------------------------------------------------------------------
# NO - OF & PP
robot16 = {'name': 'robot16',  'no_skills': 5,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject'], 'mass_capacity' : 100}

# NO - OF & S
robot17 = {'name': 'robot17',  'no_skills': 6,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 100}

# NO - OF & B
robot18 = {'name': 'robot18',  'no_skills': 6,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'SliceObject', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 100}

# NO - OF 
robot19 = {'name': 'robot19',  'no_skills': 7,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 
                                         'PickupObject', 'PutObject'], 'mass_capacity' : 100}

# ------------------------------------------------------------------------------------------------------------------------------------------------------------

# NO - PP & S
robot20 = {'name': 'robot20',  'no_skills': 6,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SwitchOn', 'SwitchOff'], 'mass_capacity' : 100}

# NO - PP & T
robot21 = {'name': 'robot21',  'no_skills': 7,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff'], 'mass_capacity' : 100}

# NO - PP 
robot22 = {'name': 'robot22',   'no_skills': 7,   'skills': ['GoToObject', 'OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'SwitchOn', 'SwitchOff'], 'mass_capacity' : 100}

# Specialists
# Only OC
robot23 = {'name': 'robot23',  'no_skills': 3,   'skills': ['GoToObject', 'OpenObject', 'CloseObject'], 'mass_capacity' : 100}

# Only OF
robot24 = {'name': 'robot24',  'no_skills': 3,   'skills': ['GoToObject','SwitchOn', 'SwitchOff'], 'mass_capacity' : 100}

# Only PP
robot25 = {'name': 'robot25',  'no_skills': 9,   'skills': ['GoToObject', 'PickupObject', 'PutObject', 'CleanObject', 'OpenObject', 'CloseObject', 'RunMicrowave', 'RunCoffeeMachine', 'RunToaster'], 'mass_capacity' : 100}

# Only S
robot26 = {'name': 'robot26',  'no_skills': 3,   'skills': ['GoToObject','SliceObject', 'PickupObject'], 'mass_capacity' : 100}

# Only T
robot27 = {'name': 'robot27',  'no_skills': 2,   'skills': ['GoToObject','BreakObject'], 'mass_capacity' : 100}

robots = [robot1, robot2, robot3, robot4, robot5, robot6, robot7, robot8, robot9, robot10,
          robot11, robot12, robot13, robot14, robot15, robot16, robot17, robot18, robot19, robot20, robot21,robot22, robot23, robot24, robot25, robot26, robot27, robot28]


