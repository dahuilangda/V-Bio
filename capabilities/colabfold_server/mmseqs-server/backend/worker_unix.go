// +build !windows

package main

import (
	"log"
	"os/exec"
	"time"

	"golang.org/x/sys/unix"
)

func SetSysProcAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &unix.SysProcAttr{Setpgid: true}
}

func KillCommand(cmd *exec.Cmd) error {
	return unix.Kill(-cmd.Process.Pid, unix.SIGKILL)
}

// TerminateCommand gives the job's process group a SIGTERM first so wrappers
// can run cleanup handlers (the mmseqs GPU wrapper releases its redis lease
// there — killing it outright leaks the lease), then escalates to SIGKILL of
// the whole group after the grace period.
func TerminateCommand(cmd *exec.Cmd, done chan error, grace time.Duration) {
	if cmd.Process == nil {
		return
	}
	if err := unix.Kill(-cmd.Process.Pid, unix.SIGTERM); err != nil {
		if err := KillCommand(cmd); err != nil {
			log.Printf("Failed to kill: %s\n", err)
		}
		return
	}
	if grace <= 0 {
		grace = 15 * time.Second
	}
	select {
	case <-done:
		return
	case <-time.After(grace):
		if err := KillCommand(cmd); err != nil {
			log.Printf("Failed to kill: %s\n", err)
		}
	}
}
