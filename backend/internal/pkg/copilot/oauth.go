package copilot

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"go.uber.org/zap"
)

const (
	GitHubClientID        = "Iv1.b507a08c87ecfe98"
	GitHubDeviceCodeURL   = "https://github.com/login/device/code"
	GitHubAccessTokenURL  = "https://github.com/login/oauth/access_token"
	GitHubAPIKeyURL       = "https://api.github.com/copilot_internal/v2/token"
	CopilotAPIBase        = "https://api.githubcopilot.com"
	CopilotVersion        = "0.26.7"
	EditorPluginVersion   = "copilot-chat/0.26.7"
	UserAgent             = "GitHubCopilotChat/0.26.7"
	APIVersion            = "2025-04-01"
)

type DeviceCodeResponse struct {
	DeviceCode      string `json:"device_code"`
	UserCode        string `json:"user_code"`
	VerificationURI string `json:"verification_uri"`
	ExpiresIn       int    `json:"expires_in"`
	Interval        int    `json:"interval"`
}

type AccessTokenResponse struct {
	AccessToken string `json:"access_token"`
	TokenType   string `json:"token_type"`
	Scope       string `json:"scope"`
	Error       string `json:"error,omitempty"`
}

type APIKeyResponse struct {
	Token     string                 `json:"token"`
	ExpiresAt int64                  `json:"expires_at"`
	Endpoints map[string]interface{} `json:"endpoints"`
}

func GetDeviceCode(ctx context.Context) (*DeviceCodeResponse, error) {
	data := url.Values{}
	data.Set("client_id", GitHubClientID)
	data.Set("scope", "user:email")

	req, err := http.NewRequestWithContext(ctx, "POST", GitHubDeviceCodeURL, nil)
	if err != nil {
		return nil, err
	}
	req.URL.RawQuery = data.Encode()
	req.Header.Set("Accept", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("failed to get device code: %s", string(body))
	}

	var result DeviceCodeResponse
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, err
	}

	return &result, nil
}

func PollForAccessToken(ctx context.Context, deviceCode string, interval int) (string, error) {
	ticker := time.NewTicker(time.Duration(interval) * time.Second)
	defer ticker.Stop()

	timeout := time.After(10 * time.Minute)

	for {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-timeout:
			return "", fmt.Errorf("timeout waiting for authorization")
		case <-ticker.C:
			data := url.Values{}
			data.Set("client_id", GitHubClientID)
			data.Set("device_code", deviceCode)
			data.Set("grant_type", "urn:ietf:params:oauth:grant-type:device_code")

			req, err := http.NewRequestWithContext(ctx, "POST", GitHubAccessTokenURL, nil)
			if err != nil {
				return "", err
			}
			req.URL.RawQuery = data.Encode()
			req.Header.Set("Accept", "application/json")

			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				logger.L().Warn("Failed to poll access token", zap.Error(err))
				continue
			}

			body, err := io.ReadAll(resp.Body)
			resp.Body.Close()
			if err != nil {
				logger.L().Warn("Failed to read response", zap.Error(err))
				continue
			}

			var result AccessTokenResponse
			if err := json.Unmarshal(body, &result); err != nil {
				logger.L().Warn("Failed to parse response", zap.Error(err))
				continue
			}

			if result.Error == "authorization_pending" {
				continue
			}

			if result.Error != "" {
				return "", fmt.Errorf("authorization error: %s", result.Error)
			}

			if result.AccessToken != "" {
				return result.AccessToken, nil
			}
		}
	}
}

func GetAPIKey(ctx context.Context, accessToken string) (*APIKeyResponse, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", GitHubAPIKeyURL, nil)
	if err != nil {
		return nil, err
	}

	req.Header.Set("Authorization", fmt.Sprintf("token %s", accessToken))
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Editor-Version", "vscode/1.95.0")
	req.Header.Set("Editor-Plugin-Version", EditorPluginVersion)
	req.Header.Set("User-Agent", UserAgent)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("failed to get API key: %s", string(body))
	}

	var result APIKeyResponse
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, err
	}

	return &result, nil
}
