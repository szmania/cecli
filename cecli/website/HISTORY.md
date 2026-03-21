## v0.XX.X (Upcoming Release)

### Features & Improvements
- **Multi-workspace Support**: Added `--workspace-paths` argument to specify multiple workspace directories
  ```bash
  # CLI usage with multiple directories
  cecli --workspace-paths /path/to/project1 --workspace-paths /path/to/project2
  
  # Environment variable
  export CECLI_WORKSPACE_PATHS="/path/to/project1:/path/to/project2"
  cecli
  
  # YAML configuration (.cecli.conf.yml)
  workspace-paths:
    - /path/to/project1
    - /path/to/project2
  ```
  
  Supports:
  - Cross-repository operations
  - Microservices architecture support
  - Multiple component directories
  - Relative and absolute paths
  - Environment variable configuration
  
  Resolution order: CLI arguments > Environment variable > YAML config > Current directory

### Developer Experience
- Extended skills framework to support multiple workspace contexts
- Various bug fixes and stability improvements

### Documentation
- Added comprehensive examples for multi-workspace configuration
- Updated CLI help and configuration templates
