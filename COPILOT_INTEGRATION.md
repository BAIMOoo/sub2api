# GitHub Copilot Integration for sub2api

## Overview

This document describes the GitHub Copilot integration added to sub2api, following the same OAuth device flow pattern used by litellm.

## Architecture

### 1. Platform Constant
- Added `PlatformCopilot = "copilot"` to `/backend/internal/domain/constants.go`

### 2. OAuth Package
- Created `/backend/internal/pkg/copilot/oauth.go`
- Implements GitHub OAuth device flow:
  - `GetDeviceCode()` - Initiates device authorization
  - `PollForAccessToken()` - Polls for user authorization
  - `GetAPIKey()` - Exchanges access token for Copilot API key

### 3. Service Layer
- Created `/backend/internal/service/copilot_oauth_service.go`
- Provides:
  - `InitiateDeviceFlow()` - Start OAuth flow
  - `CompleteDeviceFlow()` - Complete authorization and get tokens
  - `RefreshAPIKey()` - Refresh expired API keys

### 4. Handler Layer
- Created `/backend/internal/handler/copilot_oauth.go`
- HTTP handlers for OAuth endpoints

### 5. Routes
- Added to `/backend/internal/server/routes/auth.go`:
  - `POST /api/v1/auth/oauth/copilot/start` - Initiate device flow
  - `POST /api/v1/auth/oauth/copilot/complete` - Complete authorization

## API Usage

### 1. Start Device Flow

```bash
curl -X POST http://localhost:8080/api/v1/auth/oauth/copilot/start \
  -H "Content-Type: application/json"
```

Response:
```json
{
  "device_code": "...",
  "user_code": "XXXX-XXXX",
  "verification_uri": "https://github.com/login/device",
  "expires_in": 900,
  "interval": 5
}
```

### 2. User Authorization
User visits `verification_uri` and enters `user_code` to authorize.

### 3. Complete Flow

```bash
curl -X POST http://localhost:8080/api/v1/auth/oauth/copilot/complete \
  -H "Content-Type: application/json" \
  -d '{
    "device_code": "...",
    "interval": 5
  }'
```

Response:
```json
{
  "access_token": "gho_...",
  "api_key": "...",
  "expires_at": "2026-03-30T12:00:00Z"
}
```

## Implementation Details

### Authentication Flow
1. Client calls `/oauth/copilot/start`
2. Server returns device code and user code
3. User authorizes on GitHub
4. Client polls `/oauth/copilot/complete`
5. Server returns access token and API key

### Token Management
- Access tokens are long-lived GitHub OAuth tokens
- API keys expire and need periodic refresh
- Use `RefreshAPIKey()` with access token to get new API key

### Constants
- Client ID: `Iv1.b507a08c87ecfe98` (GitHub Copilot official)
- API Base: `https://api.githubcopilot.com`
- Copilot Version: `0.26.7`
- API Version: `2025-04-01`

## Next Steps

To complete the integration:

1. **Build the project**:
   ```bash
   cd backend
   go generate ./cmd/server/wire.go
   go build ./cmd/server
   ```

2. **Add account storage**:
   - Store access tokens and API keys in database
   - Link to user accounts
   - Implement token refresh logic

3. **Frontend integration**:
   - Add Copilot OAuth UI flow
   - Display device code to user
   - Poll for completion

4. **Gateway integration**:
   - Add Copilot request routing
   - Implement API key injection
   - Handle token refresh on 401 errors

## References

- [GitHub Copilot API](https://docs.github.com/en/copilot)
- [LiteLLM Copilot Integration](https://github.com/BerriAI/litellm/tree/main/litellm/llms/github_copilot)
- [OAuth Device Flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#device-flow)
