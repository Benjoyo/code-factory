"""GraphQL documents for the shared Linear ticket operations layer."""

from __future__ import annotations

ISSUE_QUERY = """
query CodeFactoryTrackerIssue(
  $id: String!,
  $includeDescription: Boolean!,
  $includeComments: Boolean!,
  $includeAttachments: Boolean!,
  $includeRelations: Boolean!
) {
  issue(id: $id) {
    id
    identifier
    title
    description @include(if: $includeDescription)
    priority
    url
    branchName
    state { id name type }
    project { id name slugId url }
    team { id name key }
    labels { nodes { id name } }
    comments(first: 50) @include(if: $includeComments) {
      nodes {
        id
        body
        createdAt
        updatedAt
        resolvedAt
        user { name }
      }
    }
    attachments(first: 20) @include(if: $includeAttachments) {
      nodes {
        id
        title
        subtitle
        url
        sourceType
        metadata
      }
    }
    inverseRelations(first: 20) @include(if: $includeRelations) {
      nodes {
        type
        issue {
          id
          identifier
          title
          state { id name type }
        }
      }
    }
    relations(first: 20) @include(if: $includeRelations) {
      nodes {
        type
        relatedIssue {
          id
          identifier
          title
          state { id name type }
        }
      }
    }
  }
}
"""

ISSUES_QUERY = """
query CodeFactoryTrackerIssues(
  $first: Int!,
  $after: String,
  $includeDescription: Boolean!,
  $includeComments: Boolean!,
  $includeAttachments: Boolean!,
  $includeRelations: Boolean!
) {
  issues(first: $first, after: $after) {
    nodes {
      id
      identifier
      title
      description @include(if: $includeDescription)
      priority
      url
      branchName
      state { id name type }
      project { id name slugId url }
      team { id name key }
      labels { nodes { id name } }
      comments(first: 20) @include(if: $includeComments) {
        nodes {
          id
          body
          createdAt
          updatedAt
          resolvedAt
          user { name }
        }
      }
      attachments(first: 10) @include(if: $includeAttachments) {
        nodes {
          id
          title
          subtitle
          url
          sourceType
          metadata
        }
      }
      inverseRelations(first: 10) @include(if: $includeRelations) {
        nodes {
          type
          issue {
            id
            identifier
            title
            state { id name type }
          }
        }
      }
      relations(first: 10) @include(if: $includeRelations) {
        nodes {
          type
          relatedIssue {
            id
            identifier
            title
            state { id name type }
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

PROJECTS_QUERY = """
query CodeFactoryTrackerProjects($first: Int!) {
  projects(first: $first) {
    nodes {
      id
      name
      slugId
      url
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

TEAMS_QUERY = """
query CodeFactoryTrackerTeams($first: Int!) {
  teams(first: $first) {
    nodes {
      id
      name
      key
      states(first: 50) { nodes { id name type } }
    }
  }
}
"""

USERS_QUERY = """
query CodeFactoryTrackerUsers($first: Int!) {
  users(first: $first) {
    nodes {
      id
      name
      displayName
      email
    }
  }
}
"""

LABELS_QUERY = """
query CodeFactoryTrackerLabels($first: Int!) {
  issueLabels(first: $first) {
    nodes {
      id
      name
    }
  }
}
"""

CREATE_ISSUE_MUTATION = """
mutation CodeFactoryTrackerCreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier title url }
  }
}
"""

UPDATE_ISSUE_MUTATION = """
mutation CodeFactoryTrackerUpdateIssue($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { id identifier title url }
  }
}
"""

CREATE_RELATION_MUTATION = """
mutation CodeFactoryTrackerCreateRelation($input: IssueRelationCreateInput!) {
  issueRelationCreate(input: $input) {
    success
  }
}
"""

COMMENT_CREATE_MUTATION = """
mutation CodeFactoryTrackerCommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id url }
  }
}
"""

COMMENT_UPDATE_MUTATION = """
mutation CodeFactoryTrackerCommentUpdate($commentId: String!, $body: String!) {
  commentUpdate(id: $commentId, input: { body: $body }) {
    success
    comment { id url }
  }
}
"""

ATTACH_PR_MUTATION = """
mutation CodeFactoryTrackerAttachPr(
  $issueId: String!,
  $url: String!,
  $title: String
) {
  attachmentLinkGitHubPR(
    issueId: $issueId,
    url: $url,
    title: $title,
    linkKind: links
  ) {
    success
    attachment { id title url }
  }
}
"""

ATTACH_LINK_FALLBACK_MUTATION = """
mutation CodeFactoryTrackerAttachFallback(
  $issueId: String!,
  $url: String!,
  $title: String!
) {
  attachmentCreate(input: { issueId: $issueId, url: $url, title: $title }) {
    success
    attachment { id title url }
  }
}
"""

FILE_UPLOAD_MUTATION = """
mutation CodeFactoryTrackerFileUpload(
  $filename: String!,
  $contentType: String!,
  $size: Int!
) {
  fileUpload(
    filename: $filename,
    contentType: $contentType,
    size: $size,
    makePublic: true
  ) {
    success
    uploadFile {
      uploadUrl
      assetUrl
      headers { key value }
    }
  }
}
"""
