import os
import json
from pathlib import Path
from typing import List, Tuple, Optional

class InitializationError(Exception):
    """Base exception class for initialization errors."""
    pass

class VerificationError(InitializationError):
    """Exception raised for verification failures."""
    pass

def verify_directories(base_path: str) -> bool:
    """Verify and create required directories.
    
    Args:
        base_path (str): Base path for directory creation
        
    Returns:
        bool: True if all directories are valid, False otherwise
        
    Raises:
        VerificationError: If directory verification fails
    """
    required_dirs = [
        os.path.join(base_path, "resources", "generated_subtask"),
        os.path.join(base_path, "resources", "each_run"),
        os.path.join(base_path, "logs")
    ]
    
    try:
        for dir_path in required_dirs:
            os.makedirs(dir_path, exist_ok=True)
            if not os.path.isdir(dir_path):
                raise VerificationError(f"Failed to create directory: {dir_path}")
        return True
        
    except Exception as e:
        raise VerificationError(f"Error verifying directories: {str(e)}")

def verify_prompt_files(base_path: str, prompt_sets: List[str]) -> bool:
    """Verify required prompt files exist.
    
    Args:
        base_path (str): Base path for prompt files
        prompt_sets (List[str]): List of prompt set names
        
    Returns:
        bool: True if all prompt files exist, False otherwise
        
    Raises:
        VerificationError: If prompt file verification fails
    """
    required_files = [
        f"{prompt_sets[0]}.py",
        f"{prompt_sets[1]}_problem.py",
        f"{prompt_sets[1]}_solution.py",
        f"{prompt_sets[1]}_summary.py",
        f"{prompt_sets[1]}_teamproblem.py"
    ]
    
    try:
        for file in required_files:
            file_path = os.path.join(base_path, "data", "pythonic_plans", file)
            if not os.path.isfile(file_path):
                raise VerificationError(f"Required prompt file not found: {file_path}")
        return True
        
    except Exception as e:
        raise VerificationError(f"Error verifying prompt files: {str(e)}")

def verify_test_data(test_file: str) -> bool:
    """Verify test data file exists and has correct structure.
    
    Args:
        test_file (str): Path to the test file
        
    Returns:
        bool: True if test data is valid, False otherwise
        
    Raises:
        VerificationError: If test data verification fails
    """
    try:
        if not os.path.isfile(test_file):
            raise VerificationError(f"Test file not found: {test_file}")
            
        with open(test_file, 'r') as f:
            data = json.load(f)
            required_fields = ['task', 'robot list', 'object_states', 'trans', 'max_trans']
            for field in required_fields:
                if field not in data:
                    raise VerificationError(f"Missing required field in test file: {field}")
        return True
        
    except json.JSONDecodeError as e:
        raise VerificationError(f"Invalid JSON in test file: {str(e)}")
    except Exception as e:
        raise VerificationError(f"Error verifying test data: {str(e)}")

def verify_robot_pddl_files(base_path: str, robot_ids: List[int]) -> bool:
    """Verify robot PDDL files exist.
    
    Args:
        base_path (str): Base path for robot PDDL files
        robot_ids (List[int]): List of robot IDs to verify
        
    Returns:
        bool: True if all robot PDDL files exist, False otherwise
        
    Raises:
        VerificationError: If robot PDDL file verification fails
    """
    try:
        for robot_id in robot_ids:
            pddl_file = os.path.join(base_path, "resources", f"robot{robot_id}.pddl")
            if not os.path.isfile(pddl_file):
                raise VerificationError(f"Robot PDDL file not found: {pddl_file}")
        return True
        
    except Exception as e:
        raise VerificationError(f"Error verifying robot PDDL files: {str(e)}")

def verify_all(base_path: str, prompt_sets: List[str], test_file: str, robot_ids: List[int]) -> Tuple[bool, Optional[str]]:
    """Verify all initialization requirements.
    
    Args:
        base_path (str): Base path for all operations
        prompt_sets (List[str]): List of prompt set names
        test_file (str): Path to test file
        robot_ids (List[int]): List of robot IDs to verify
        
    Returns:
        Tuple[bool, Optional[str]]: (success status, error message if any)
    """
    try:
        # Verify directories
        verify_directories(base_path)
        
        # Verify prompt files
        verify_prompt_files(base_path, prompt_sets)
        
        # Verify test data
        verify_test_data(test_file)
        
        # Verify robot PDDL files
        verify_robot_pddl_files(base_path, robot_ids)
        
        return True, None
        
    except VerificationError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Unexpected error during verification: {str(e)}" 