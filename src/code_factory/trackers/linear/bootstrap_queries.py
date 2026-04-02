"""GraphQL documents used by the Linear bootstrap provisioning helper."""

from __future__ import annotations

CREATE_PROJECT_MUTATION = """
mutation CodeFactoryBootstrapCreateProject($input: ProjectCreateInput!) {
  projectCreate(input: $input) {
    success
    project {
      id
      name
      slugId
      teams(first: 20) {
        nodes {
          id
          name
          key
          states(first: 50) { nodes { id name type } }
        }
      }
    }
  }
}
"""

CREATE_WORKFLOW_STATE_MUTATION = """
mutation CodeFactoryBootstrapCreateWorkflowState($input: WorkflowStateCreateInput!) {
  workflowStateCreate(input: $input) {
    success
    workflowState { id name type }
  }
}
"""
