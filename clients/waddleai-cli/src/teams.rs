//! `waddleai teams ...` -- team and membership management.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::pretty;

/// `waddleai teams` subcommands.
#[derive(Debug, Subcommand)]
pub enum TeamsCommand {
    /// List teams.
    List,
    /// Get a team by ID.
    Get { team_id: i64 },
    /// Create a team.
    Create {
        #[arg(long)]
        name: String,
    },
    /// Update a team.
    Update {
        team_id: i64,
        #[arg(long)]
        name: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
    },
    /// Delete a team.
    Delete { team_id: i64 },
    /// Manage team members.
    Members {
        #[command(subcommand)]
        action: TeamMembersCommand,
    },
    /// Transfer team ownership to another user.
    TransferOwnership {
        team_id: i64,
        #[arg(long)]
        new_owner_user_id: i64,
    },
}

#[derive(Debug, Subcommand)]
pub enum TeamMembersCommand {
    /// List members of a team.
    List { team_id: i64 },
    /// Add a member to a team.
    Add {
        team_id: i64,
        #[arg(long)]
        user_id: i64,
        #[arg(long)]
        team_role: Option<String>,
    },
    /// Set a member's role in a team.
    SetRole {
        team_id: i64,
        user_id: i64,
        #[arg(long)]
        team_role: String,
    },
    /// Remove a member from a team.
    Remove { team_id: i64, user_id: i64 },
}

pub async fn run(api: &ApiClient, action: TeamsCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: TeamsCommand) -> Result<String> {
    match action {
        TeamsCommand::List => {
            let val: Value = api.get_json("/api/v1/teams").await?;
            pretty(&val)
        }
        TeamsCommand::Get { team_id } => {
            let val: Value = api.get_json(&format!("/api/v1/teams/{team_id}")).await?;
            pretty(&val)
        }
        TeamsCommand::Create { name } => {
            let val: Value = api
                .post_json("/api/v1/teams", &json!({ "name": name }))
                .await?;
            pretty(&val)
        }
        TeamsCommand::Update {
            team_id,
            name,
            enabled,
        } => {
            let mut body = Map::new();
            if let Some(v) = name {
                body.insert("name".into(), json!(v));
            }
            if let Some(v) = enabled {
                body.insert("enabled".into(), json!(v));
            }
            let val: Value = api
                .put_json(&format!("/api/v1/teams/{team_id}"), &Value::Object(body))
                .await?;
            pretty(&val)
        }
        TeamsCommand::Delete { team_id } => {
            let val: Value = api.delete_json(&format!("/api/v1/teams/{team_id}")).await?;
            pretty(&val)
        }
        TeamsCommand::Members { action } => match action {
            TeamMembersCommand::List { team_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/teams/{team_id}/members"))
                    .await?;
                pretty(&val)
            }
            TeamMembersCommand::Add {
                team_id,
                user_id,
                team_role,
            } => {
                let mut body = Map::new();
                body.insert("user_id".into(), json!(user_id));
                if let Some(v) = team_role {
                    body.insert("team_role".into(), json!(v));
                }
                let val: Value = api
                    .post_json(
                        &format!("/api/v1/teams/{team_id}/members"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            TeamMembersCommand::SetRole {
                team_id,
                user_id,
                team_role,
            } => {
                let val: Value = api
                    .put_json(
                        &format!("/api/v1/teams/{team_id}/members/{user_id}"),
                        &json!({ "team_role": team_role }),
                    )
                    .await?;
                pretty(&val)
            }
            TeamMembersCommand::Remove { team_id, user_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/teams/{team_id}/members/{user_id}"))
                    .await?;
                pretty(&val)
            }
        },
        TeamsCommand::TransferOwnership {
            team_id,
            new_owner_user_id,
        } => {
            let val: Value = api
                .post_json(
                    &format!("/api/v1/teams/{team_id}/transfer-ownership"),
                    &json!({ "new_owner_user_id": new_owner_user_id }),
                )
                .await?;
            pretty(&val)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn client(server: &MockServer) -> ApiClient {
        ApiClient::new(server.uri(), Some("at-test".into())).expect("client")
    }

    #[tokio::test]
    async fn teams_and_members_crud() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/teams"),
            ("GET", "/api/v1/teams/3"),
            ("POST", "/api/v1/teams"),
            ("PUT", "/api/v1/teams/3"),
            ("DELETE", "/api/v1/teams/3"),
            ("GET", "/api/v1/teams/3/members"),
            ("POST", "/api/v1/teams/3/members"),
            ("PUT", "/api/v1/teams/3/members/10"),
            ("DELETE", "/api/v1/teams/3/members/10"),
            ("POST", "/api/v1/teams/3/transfer-ownership"),
        ] {
            Mock::given(method(m))
                .and(path(p))
                .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
                .mount(&server)
                .await;
        }
        let c = client(&server);
        assert!(execute(&c, TeamsCommand::List).await.is_ok());
        assert!(execute(&c, TeamsCommand::Get { team_id: 3 }).await.is_ok());
        assert!(execute(&c, TeamsCommand::Create { name: "eng".into() })
            .await
            .is_ok());
        assert!(execute(
            &c,
            TeamsCommand::Update {
                team_id: 3,
                name: Some("eng2".into()),
                enabled: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            TeamsCommand::Members {
                action: TeamMembersCommand::List { team_id: 3 }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            TeamsCommand::Members {
                action: TeamMembersCommand::Add {
                    team_id: 3,
                    user_id: 10,
                    team_role: Some("member".into()),
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            TeamsCommand::Members {
                action: TeamMembersCommand::SetRole {
                    team_id: 3,
                    user_id: 10,
                    team_role: "admin".into(),
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            TeamsCommand::Members {
                action: TeamMembersCommand::Remove {
                    team_id: 3,
                    user_id: 10,
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            TeamsCommand::TransferOwnership {
                team_id: 3,
                new_owner_user_id: 11,
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, TeamsCommand::Delete { team_id: 3 })
            .await
            .is_ok());
    }
}
