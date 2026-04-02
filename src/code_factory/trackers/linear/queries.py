"""Hardcoded GraphQL documents used by the Linear tracker client."""

QUERY = """
query CodeFactoryLinearPoll($projectId: String!, $stateNames: [String!]!, $first: Int!, $relationFirst: Int!, $after: String) {
  issues(filter: {project: {id: {eq: $projectId}}, state: {name: {in: $stateNames}}}, first: $first, after: $after) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      assignee { id }
      labels { nodes { name } }
      inverseRelations(first: $relationFirst) {
        nodes {
          type
          issue {
            id
            identifier
            state { name }
          }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

QUERY_BY_IDS = """
query CodeFactoryLinearIssuesById($ids: [ID!]!, $first: Int!, $relationFirst: Int!) {
  issues(filter: {id: {in: $ids}}, first: $first) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      assignee { id }
      labels { nodes { name } }
      inverseRelations(first: $relationFirst) {
        nodes {
          type
          issue {
            id
            identifier
            state { name }
          }
        }
      }
      createdAt
      updatedAt
    }
  }
}
"""

QUERY_BY_IDENTIFIER = """
query CodeFactoryLinearIssueByIdentifier($identifier: String!, $relationFirst: Int!) {
  issue(id: $identifier) {
    id
    identifier
    title
    description
    priority
    state { name }
    branchName
    url
    assignee { id }
    labels { nodes { name } }
    inverseRelations(first: $relationFirst) {
      nodes {
        type
        issue {
          id
          identifier
          state { name }
        }
      }
    }
    createdAt
    updatedAt
  }
}
"""

COMMENTS_QUERY = """
query CodeFactoryIssueComments($issueId: String!, $first: Int!, $after: String) {
  issue(id: $issueId) {
    comments(first: $first, after: $after) {
      nodes {
        id
        body
        createdAt
        updatedAt
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

VIEWER_QUERY = """
query CodeFactoryLinearViewer {
  viewer { id }
}
"""

CREATE_COMMENT_MUTATION = """
mutation CodeFactoryCreateComment($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) { success }
}
"""

UPDATE_COMMENT_MUTATION = """
mutation CodeFactoryUpdateComment($commentId: String!, $body: String!) {
  commentUpdate(id: $commentId, input: {body: $body}) { success }
}
"""

UPDATE_STATE_MUTATION = """
mutation CodeFactoryUpdateIssueState($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: {stateId: $stateId}) { success }
}
"""

STATE_LOOKUP_QUERY = """
query CodeFactoryResolveStateId($issueId: String!, $stateName: String!) {
  issue(id: $issueId) {
    team {
      states(filter: {name: {eq: $stateName}}, first: 1) {
        nodes { id }
      }
    }
  }
}
"""
