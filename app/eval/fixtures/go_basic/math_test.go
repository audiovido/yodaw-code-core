package main

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
