// +build windows

package main

import (
	"os/exec"
	"time"
)

func SetSysProcAttr(cmd *exec.Cmd) {
}

func KillCommand(cmd *exec.Cmd) error {
	return cmd.Process.Kill()
}

func TerminateCommand(cmd *exec.Cmd, done chan error, grace time.Duration) {
	if err := KillCommand(cmd); err != nil {
		return
	}
	select {
	case <-done:
	case <-time.After(grace):
	}
}
