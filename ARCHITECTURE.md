# YODAW Architecture

Client
  |
  v
Single Base URL
  |
  v
/api/v1/*
  |
  v
Mission Core
  |
  +--> Planner
  +--> Router
  +--> Worker Registry
  +--> Validator
  +--> Evidence
  +--> State
  +--> Recovery
  |
  v
Code Bud

Future workers attach through the worker contract.

The Core must not import or depend directly on Unreal Engine.
