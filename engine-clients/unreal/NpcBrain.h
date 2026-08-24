// NpcBrain.h — Unreal 客户端：把 NPCSidekick 大脑接进 UE 游戏（FHttpModule）
// 用法：Actor 持有一个 NpcBrain 组件，填 NpcId；对话调 Talk()，Tick 里轮询镜像。
#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "NpcBrain.generated.h"

DECLARE_DYNAMIC_MULTICAST_DELEGATE_OneParam(FNpcReply, const FString&, ReplyText);
DECLARE_DYNAMIC_MULTICAST_DELEGATE_OneParam(FNpcAudio, const TArray<uint8>&, Mp3Bytes);

UCLASS(ClassGroup = (NPCSidekick), meta = (BlueprintSpawnableComponent))
class NPCBRAIN_API UNpcBrain : public UActorComponent
{
	GENERATED_BODY()

public:
	UNpcBrain();

	// 服务器地址（勿带末尾斜杠）
	UPROPERTY(EditAnywhere, Category = "NpcBrain")
	FString ServerUrl = TEXT("http://127.0.0.1:8765");

	// 村民 id（服务器按它注入人格）
	UPROPERTY(EditAnywhere, Category = "NpcBrain")
	FString NpcId = TEXT("cang");

	// 请求语音
	UPROPERTY(EditAnywhere, Category = "NpcBrain")
	bool bVoice = true;

	UPROPERTY(BlueprintAssignable, Category = "NpcBrain")
	FNpcReply OnReply;

	UPROPERTY(BlueprintAssignable, Category = "NpcBrain")
	FNpcAudio OnAudio;

	// 玩家输入一句 → 发到 /api/talk
	UFUNCTION(BlueprintCallable, Category = "NpcBrain")
	void Talk(const FString& Message);

	// 最新世界状态快照（表演逻辑读）
	UPROPERTY(BlueprintReadOnly, Category = "NpcBrain")
	FString Position;

	UPROPERTY(BlueprintReadOnly, Category = "NpcBrain")
	FString State = TEXT("idle");

	virtual void TickComponent(float DeltaTime, ELevelTick TickType,
		FActorComponentTickFunction* ThisTickFunction) override;

private:
	float PollTimer = 0.f;
	void PollState();
};
