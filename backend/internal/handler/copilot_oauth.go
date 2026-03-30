package handler

import (
	"net/http"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

type CopilotOAuthHandler struct {
	copilotOAuthService *service.CopilotOAuthService
}

func NewCopilotOAuthHandler(copilotOAuthService *service.CopilotOAuthService) *CopilotOAuthHandler {
	return &CopilotOAuthHandler{
		copilotOAuthService: copilotOAuthService,
	}
}

func (h *CopilotOAuthHandler) InitiateDeviceFlow(c *gin.Context) {
	result, err := h.copilotOAuthService.InitiateDeviceFlow(c.Request.Context())
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, result)
}

type CompleteDeviceFlowRequest struct {
	DeviceCode string `json:"device_code" binding:"required"`
	Interval   int    `json:"interval" binding:"required"`
}

func (h *CopilotOAuthHandler) CompleteDeviceFlow(c *gin.Context) {
	var req CompleteDeviceFlowRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	result, err := h.copilotOAuthService.CompleteDeviceFlow(c.Request.Context(), req.DeviceCode, req.Interval)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, result)
}
