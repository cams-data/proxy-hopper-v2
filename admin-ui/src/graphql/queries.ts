import { gql } from "urql";

export const STATUS_QUERY = gql`
  query Status {
    status {
      authEnabled
      userSub
      userRole
      readOnly
    }
  }
`;

export const PROVIDERS_QUERY = gql`
  query Providers {
    providers {
      name
      ipList
      regionTag
      hasAuth
      static
      mutable
    }
  }
`;

export const POOLS_QUERY = gql`
  query Pools {
    pools {
      name
      static
      mutable
      ipRequests {
        provider
        count
      }
    }
  }
`;

export const TARGETS_QUERY = gql`
  query Targets {
    targets {
      name
      regex
      poolName
      minRequestInterval
      maxQueueWait
      numRetries
      ipFailuresUntilQuarantine
      quarantineTime
      defaultProxyPort
      spoofUserAgent
      static
      mutable
      resolvedIps {
        host
        port
        provider
      }
    }
  }
`;

export const TARGET_IP_STATES_QUERY = gql`
  query TargetIpStates($targetName: String!) {
    targetIpStates(targetName: $targetName) {
      address
      host
      port
      provider
      failures
      quarantined
      releaseAt
      userAgent
      requestCount
      cookiesEnabled
      profileHeaders {
        name
        value
      }
      cookies {
        name
        value
      }
      identityEnabled
    }
  }
`;

export const TARGET_METRICS_QUERY = gql`
  query TargetMetrics($name: String!) {
    targetMetrics(name: $name) {
      name
      totalRequests
      successRequests
      failedRequests
      avgLatencyMs
      lastRequestAt
    }
  }
`;
