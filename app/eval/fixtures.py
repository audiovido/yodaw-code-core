"""
Repository fixtures for benchmark evaluation.
"""
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional


class FixtureManager:
    """Manage disposable benchmark repository fixtures."""
    
    def __init__(self, fixtures_dir: Optional[str] = None):
        if fixtures_dir is None:
            fixtures_dir = os.path.join(
                os.path.dirname(__file__), "fixtures"
            )
        self.fixtures_dir = Path(fixtures_dir)
    
    def create_temp_repo(self, fixture_name: str) -> Path:
        """Create a temporary copy of a fixture repository."""
        fixture_path = self.fixtures_dir / fixture_name
        if not fixture_path.exists():
            raise ValueError(f"Fixture not found: {fixture_name}")
        
        temp_dir = tempfile.mkdtemp(prefix=f"yodaw_bench_{fixture_name}_")
        temp_path = Path(temp_dir)
        
        # Copy fixture to temp location
        shutil.copytree(fixture_path, temp_path / "repo")
        
        # Initialize git if not already initialized
        repo_path = temp_path / "repo"
        if not (repo_path / ".git").exists():
            os.system(f"cd {repo_path} && git init && git add . && git commit -m 'initial'")
        
        return repo_path
    
    def cleanup_temp_repo(self, repo_path: Path):
        """Clean up temporary repository."""
        if repo_path.exists():
            shutil.rmtree(repo_path.parent)


def create_python_fixture():
    """Create Python benchmark fixture."""
    fixtures_dir = Path(__file__).parent / "fixtures" / "python_basic"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    
    # Simple calculator module
    (fixtures_dir / "calculator.py").write_text("""
def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b

def divide(a, b):
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
""")
    
    # Test file with bug
    (fixtures_dir / "test_calculator.py").write_text("""
import pytest
from calculator import add, subtract, multiply, divide

def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0

def test_subtract():
    assert subtract(5, 3) == 2
    assert subtract(0, 5) == -5

def test_multiply():
    assert multiply(3, 4) == 12
    assert multiply(-2, 3) == -6

def test_divide():
    assert divide(10, 2) == 5
    assert divide(7, 2) == 3.5
    
def test_divide_by_zero():
    with pytest.raises(ValueError):
        divide(5, 0)
""")
    
    # requirements.txt
    (fixtures_dir / "requirements.txt").write_text("pytest\n")
    
    # README
    (fixtures_dir / "README.md").write_text("""# Calculator
Simple calculator module for testing.
""")


def create_node_fixture():
    """Create Node/TypeScript benchmark fixture."""
    fixtures_dir = Path(__file__).parent / "fixtures" / "node_basic"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    
    # Simple util module
    (fixtures_dir / "utils.js").write_text("""
function capitalize(str) {
    if (!str) return '';
    return str.charAt(0).toUpperCase() + str.slice(1);
}

function reverse(str) {
    return str.split('').reverse().join('');
}

function isPalindrome(str) {
    const cleaned = str.toLowerCase().replace(/[^a-z0-9]/g, '');
    return cleaned === reverse(cleaned);
}

module.exports = { capitalize, reverse, isPalindrome };
""")
    
    # Test file
    (fixtures_dir / "utils.test.js").write_text("""
const { capitalize, reverse, isPalindrome } = require('./utils');

test('capitalize empty string', () => {
    expect(capitalize('')).toBe('');
});

test('capitalize normal string', () => {
    expect(capitalize('hello')).toBe('Hello');
});

test('reverse string', () => {
    expect(reverse('hello')).toBe('olleh');
});

test('isPalindrome basic', () => {
    expect(isPalindrome('racecar')).toBe(true);
    expect(isPalindrome('hello')).toBe(false);
});
""")
    
    # package.json
    (fixtures_dir / "package.json").write_text("""{
  "name": "utils-test",
  "version": "1.0.0",
  "scripts": {
    "test": "jest"
  },
  "devDependencies": {
    "jest": "^29.0.0"
  }
}
""")


def create_go_fixture():
    """Create Go benchmark fixture."""
    fixtures_dir = Path(__file__).parent / "fixtures" / "go_basic"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    
    # Simple math module
    (fixtures_dir / "math.go").write_text("""package main

func Max(a, b int) int {
    if a > b {
        return a
    }
    return b
}

func Min(a, b int) int {
    if a < b {
        return a
    }
    return b
}

func Abs(n int) int {
    if n < 0 {
        return -n
    }
    return n
}
""")
    
    # Test file
    (fixtures_dir / "math_test.go").write_text("""package main

import "testing"

func TestMax(t *testing.T) {
    if Max(3, 5) != 5 {
        t.Error("Max(3, 5) should be 5")
    }
    if Max(5, 3) != 5 {
        t.Error("Max(5, 3) should be 5")
    }
}

func TestMin(t *testing.T) {
    if Min(3, 5) != 3 {
        t.Error("Min(3, 5) should be 3")
    }
    if Min(5, 3) != 3 {
        t.Error("Min(5, 3) should be 3")
    }
}

func TestAbs(t *testing.T) {
    if Abs(-5) != 5 {
        t.Error("Abs(-5) should be 5")
    }
    if Abs(5) != 5 {
        t.Error("Abs(5) should be 5")
    }
}
""")
    
    # go.mod
    (fixtures_dir / "go.mod").write_text("""module example.com/math

go 1.21
""")


def create_rust_fixture():
    """Create Rust benchmark fixture."""
    fixtures_dir = Path(__file__).parent / "fixtures" / "rust_basic"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    
    # Cargo.toml
    (fixtures_dir / "Cargo.toml").write_text("""[package]
name = "math_lib"
version = "0.1.0"
edition = "2021"
""")
    
    # Create src directory
    (fixtures_dir / "src").mkdir(exist_ok=True)
    
    # Simple math library
    (fixtures_dir / "src" / "lib.rs").write_text("""pub fn factorial(n: u64) -> u64 {
    if n == 0 {
        1
    } else {
        n * factorial(n - 1)
    }
}

pub fn fibonacci(n: u64) -> u64 {
    match n {
        0 => 0,
        1 => 1,
        _ => fibonacci(n - 1) + fibonacci(n - 2),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_factorial() {
        assert_eq!(factorial(0), 1);
        assert_eq!(factorial(1), 1);
        assert_eq!(factorial(5), 120);
    }

    #[test]
    fn test_fibonacci() {
        assert_eq!(fibonacci(0), 0);
        assert_eq!(fibonacci(1), 1);
        assert_eq!(fibonacci(6), 8);
    }
}
""")


def create_mixed_fixture():
    """Create mixed language fixture."""
    fixtures_dir = Path(__file__).parent / "fixtures" / "mixed_repo"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    
    # Python API
    (fixtures_dir / "api.py").write_text("""
def get_greeting(name):
    return f"Hello, {name}!"
""")
    
    # JavaScript frontend
    (fixtures_dir / "frontend.js").write_text("""
function displayGreeting(name) {
    const greeting = `Hello, ${name}!`;
    console.log(greeting);
    return greeting;
}

module.exports = { displayGreeting };
""")
    
    # Config file
    (fixtures_dir / "config.json").write_text("""{
  "app_name": "Greeter",
  "version": "1.0.0"
}
""")
    
    # Test
    (fixtures_dir / "test_api.py").write_text("""
from api import get_greeting

def test_greeting():
    assert get_greeting("World") == "Hello, World!"
""")


if __name__ == "__main__":
    print("Creating benchmark fixtures...")
    create_python_fixture()
    create_node_fixture()
    create_go_fixture()
    create_rust_fixture()
    create_mixed_fixture()
    print("Fixtures created successfully")
