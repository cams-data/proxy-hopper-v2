import { gql } from "urql";

export const ADD_PROVIDER = gql`
  mutation AddProvider($input: ProviderInput!) {
    addProvider(input: $input) {
      name
      ipList
      regionTag
      hasAuth
      static
      mutable
    }
  }
`;

export const UPDATE_PROVIDER = gql`
  mutation UpdateProvider($input: ProviderInput!) {
    updateProvider(input: $input) {
      name
      ipList
      regionTag
      hasAuth
      static
      mutable
    }
  }
`;

export const REMOVE_PROVIDER = gql`
  mutation RemoveProvider($name: String!) {
    removeProvider(name: $name)
  }
`;

export const ADD_POOL = gql`
  mutation AddPool($input: IpPoolInput!) {
    addPool(input: $input) {
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

export const UPDATE_POOL = gql`
  mutation UpdatePool($input: IpPoolInput!) {
    updatePool(input: $input) {
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

export const REMOVE_POOL = gql`
  mutation RemovePool($name: String!) {
    removePool(name: $name)
  }
`;

export const ADD_TARGET = gql`
  mutation AddTarget($input: TargetInput!) {
    addTarget(input: $input) {
      name
      regex
      poolName
      minRequestInterval
      maxQueueWait
      numRetries
      spoofUserAgent
      defaultProxyPort
      static
      mutable
    }
  }
`;

export const UPDATE_TARGET = gql`
  mutation UpdateTarget($input: TargetInput!) {
    updateTarget(input: $input) {
      name
      regex
      poolName
      minRequestInterval
      maxQueueWait
      numRetries
      spoofUserAgent
      defaultProxyPort
      static
      mutable
    }
  }
`;

export const REMOVE_TARGET = gql`
  mutation RemoveTarget($name: String!) {
    removeTarget(name: $name)
  }
`;
