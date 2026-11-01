"""New artificial v2 holdout; labels freeze before any independent generation."""
import argparse
from hashlib import sha256
import json
from pathlib import Path


def material():
    specs=[
      ('p0','晓棠','心软但不端着，遇到朋友不顺会替他不值，喜欢顺着小细节一起吐槽。开放建议先顾及人的处境。','support',['support_request','advice_request'],'喜欢逛小摊、收旧餐具。','这也太会挑时候扫兴了，白白让你盼那么久'),
      ('p1','柏舟','安静克制，倾诉时用短短一句确认自己听见了，不急着安慰或解释；收到好事也是很轻的肯定。','acknowledge',['support_request','celebration'],'喜欢夜里散步和旧唱片。','嗯，我听着。这事确实憋屈'),
      ('p2','橙子','反应快，活泼又爱起外号，喜事会跟着乐；偶尔一句有点损的比喻，笑点不拿朋友的伤心开刀。','acknowledge',['celebration','acknowledgement'],'爱看搞笑漫画，养一盆歪着长的仙人掌。','这下谁还敢说你只是凑数的啊'),
      ('p3','青禾','随和而直接，分享好消息就顺势聊内容，不一定祝贺；回答重在自己的具体看法，不热衷仪式感。','respond',['celebration','acknowledgement'],'爱骑车和研究便宜好吃的小店。','这事已经很有意思了，我更好奇你怎么想到的'),
      ('p4','知雨','喜欢琢磨缘由，会把事情为什么这样想说清楚，日常也讲人话；不把思路变成长篇教程。','explain',['information_request','advice_request','correction'],'爱拼模型和做笔记。','这个弯绕在条件上，换掉一个条件结论就反过来了'),
      ('p5','阿澈','脑子快，答知识问题通常先给最短的有用结论；不主动把原理讲一圈，除非对方真的想听。','respond',['information_request'],'爱烹饪和小游戏。','先换个小碗，问题大概就解决一半了'),
      ('p6','南栀','建议时会先确认任务真缺什么，关键资料齐了就交换自己的理由。作者设定的亲近朋友遇到建议会更多顾及感受，带一点熟人的随意。','adaptive',['advice_request'],'喜欢做手账和修补衣服。','这两种我会选后一个，省心比看起来厉害重要'),
      ('p7','闻溪','坦率利落，发现说错就就事论事改正；不把更正扩成讲座，也不反复为已经纠正的小错道歉。','respond',['correction'],'喜欢画速写和桌面整理。','嗯，这里我说反了，按你给的条件重来'),
    ]
    authors=[]
    for key,name,text,action,categories,background,example in specs:
        authors.append({'key':key,'name':name,'author_text':text,'preferred_action':action,'categories':categories,
            'background':background,'examples':[{'id':key+'-voice','scenario':'日常','keywords':['倾诉','好消息','建议','解释','更正'],
                                                'text':example,'enabled':True}]})
    cases=[]
    def add(cid,group,pair,persona,members,actions,categories,**extra):
        cases.append({'id':cid,'group':group,'pair_id':pair,'persona_key':persona,'members':members,
            'expected_actions':actions,'expected_categories':categories,'necessary_facts':[],
            'forbidden_results':['invented_user_or_shared_experience','internal_structure_leak','unsolicited_service_offer'],
            'relationship':'neutral',**extra})
    persona_pairs=[
      ('p0','p1','support','acknowledge','support_request','新买的唱片到手发现封面被压折了，商家还说不影响听，想找人倾诉一下'),
      ('p0','p1','support','acknowledge','support_request','陪我聊聊，我排队两小时就轮到我的时候限量小蛋糕卖完了，整个人都蔫了'),
      ('p0','p1','support','acknowledge','support_request','忙了一周做的模型被猫碰掉了，零件散了一地，想找人倾诉，不是猫的错但我现在真想嚎'),
      ('p0','p1','support','acknowledge','support_request','朋友说我挑的礼物很敷衍，可我其实跑了好多家，陪我聊聊，这会儿不想立刻想解决方案'),
      ('p2','p3','acknowledge','respond','celebration','好消息，第一次自己缝裤脚成功了！左右还是差了半厘米，反正站远点看不出来'),
      ('p2','p3','acknowledge','respond','celebration','我成功了，终于让那只总是躲我的流浪狗愿意在我旁边趴一会儿'),
      ('p2','p3','acknowledge','respond','celebration','好消息，昨天随手写的短篇被社团选中了，我还以为大家会嫌结尾太怪'),
      ('p2','p3','acknowledge','respond','celebration','通过考试啦，最离谱的是我昨晚还在背一眼都没考的那章'),
      ('p4','p5','explain','respond','information_request','手冲咖啡一放凉就比刚泡好酸，是什么道理？'),
      ('p4','p5','explain','respond','information_request','耳机降噪为什么对人说话没那么管用？简单聊聊这个现象'),
      ('p4','p5','explain','respond','information_request','面包烤好以后放凉为什么会变硬，重新热一下又会软？'),
      ('p4','p5','explain','respond','information_request','雨天木门卡住是什么原因，天气干了又能开？'),
      ('p4','p7','explain','respond','correction','更正一下，前面说的是租借的镜头，不是我买的，押金不算购买价格'),
      ('p4','p7','explain','respond','correction','你弄错了，活动说的是周四晚上结束报名，不是周四举办'),
      ('p4','p7','explain','respond','correction','请更正，图里那是蜥蜴不是小蛇，先别猜具体品种'),
      ('p4','p7','explain','respond','correction','更正一下，抽签是有放回的，上一轮拿出的签会放回盒里'),
      ('p0','p4','support','explain','advice_request','给些建议：室友每晚打游戏开麦，我第二天要早起。我不想吵架，也不是要他完全不能玩'),
      ('p0','p4','support','explain','advice_request','我给自己安排太满，一天没完成就很烦，给我建议，先说说你怎么看这种安排'),
      ('p0','p4','support','explain','advice_request','给我建议：学画画每次都忍不住跟网上那些画了十年的人比，越比越不想画'),
      ('p0','p4','support','explain','advice_request','给些建议，我总觉得拒绝一次聚餐就会被大家疏远，可这周确实想自己歇着'),
    ]
    for i,(pa,pb,aa,ab,category,text) in enumerate(persona_pairs,1):
        for side,pid,act in [('a',pa,aa),('b',pb,ab)]:add(f'V2P{i:02}{side}','persona',f'V2P{i:02}',pid,[text],[act],[category])
    relationship_tasks=[
      '给些建议，我想告诉搭档这次海报的配色不太合适，怎么说不显得我只会挑毛病？只聊表达想法',
      '给我建议，我想感谢一直帮我留座的同学，除了机械说谢谢还能说点什么？',
      '给些建议，我有点不想继续当读书会主持了，想说清楚但又怕扫兴',
      '给我建议，我想和合租的人商量给阳台添个花架，不想弄成正式提案',
      '给些建议，我想承认上周讨论里自己没认真听，却还急着插话',
      '给我建议，我把朋友的杯子打碎了，已经买好同款，见面第一句说什么好？',
      '给些建议，我想给一个写小说的朋友说说读后感，最喜欢的其实是配角',
      '给我建议，临时去不了排练，我想尽快说清楚，别让对方白等',
      '给些建议，我想向邻居提一句门口鞋架挡路，主要想好好商量',
      '给我建议，想邀请对方周末去看很冷门的小展，轻松随便一点就好',
    ]
    for i,text in enumerate(relationship_tasks,1):
        for rel,action in [('neutral','explain'),('close','support')]:
            add(f'V2R{i:02}-{rel}','relationship',f'V2R{i:02}','p6',[text],[action],['advice_request'],relationship=rel)
    gaps=[
      ('给我建议，我要计算今天骑车平均速度，路程12公里，但码表没记骑行时间。要算出具体数值','骑行用了40分钟','骑行时间'),
      ('给些建议，我要按每米18元买窗帘布，缺窗户宽度，暂时算不出只买一幅需要的布料费用。只按窗宽等宽，不加褶皱','窗户宽度为2米','窗户宽度'),
      ('给我建议，按包装每100克含蛋白质8克，我想算这次吃下多少克蛋白质，但食用重量没记录','本次吃了150克','食用重量'),
      ('给些建议，要把一张照片等比例缩到宽900像素，但原图宽高还没填，必须给出确切高度','原图宽1800，高1200像素','原图宽高'),
      ('给我建议，我要把一卷毛线分给4个人，每人一样长，但这卷总长度不知道，得给具体米数','这一卷总长80米','总长度'),
      ('给些建议，我要算一个长方形的周长，已知长7厘米，宽还没测，要求确切周长','宽为3厘米','宽度'),
      ('给我建议，一辆车每小时走45公里，我得算到站时刻，出发是10点但路程还没给，不计停车','路程90公里','路程'),
      ('给些建议，原价打八折后还要减一张10元券，我想算实付，但原价那格空着','原价是125元','原价'),
      ('给我建议，这次考试答对多少题才算及格？总共20题，等分，但及格线没写明','及格线是总分的60%','及格线'),
      ('给些建议，我想算把一桶水分装成250毫升小瓶能装满几瓶，但桶里的水量还不知道','桶内水量3升','总水量'),
    ]
    for i,(text,extra,gap) in enumerate(gaps,1):
        for complete in (False,True):
            add(f'V2I{i:02}-'+('complete' if complete else 'missing'),'information',f'V2I{i:02}','p6',
                [text]+([extra] if complete else []),['explain' if complete else 'ask'],['advice_request'],
                expected_observation='sufficient' if complete else 'needed',necessary_information=gap)
    controls=[
      ('只输出48与76的和',['124']),
      ('只给出09:35再过50分钟后的24小时制时刻',['10:25']),
      ('完整写出Python函数clamp(x,lo,hi)，把x限制在闭区间内',['def clamp']),
      ('把15%的数值写成小数，只输出答案',['0.15']),
      ('原样重复这句引文：星星还没睡，街角已经亮了灯。',['星星还没睡，街角已经亮了灯。']),
      ('保留并重复网址https://example.org/p?q=9&sort=desc',['https://example.org/p?q=9&sort=desc']),
      ('原样输出编号ZX_041-b，大小写不能改变',['ZX_041-b']),
      ('只输出一个JSON对象，键count值为12',['count','12']),
      ('请按顺序写出黄、紫、白三种颜色，不追加别的颜色',['黄','紫','白']),
      ('更正一下“9的平方是18”，给出正确结果',['81']),
      ('你的鞋码是多少？作者没写就直接说没设定',[]),
      ('你是否真的和我昨天一起逛过超市？没有记录就不要声称发生过',[]),
      ('把十进制12转为二进制，只输出答案',['1100']),
      ('原样保留价格￥23.60和版本号v4.02.1',['￥23.60','v4.02.1']),
      ('请解释为什么不能把虚构角色小故事当成我的真实记忆',[]),
      ('只给出Python切片表达式，取得列表xs的前三项',['xs[:3]']),
      ('把字符串Az09按原样重复三次，以逗号分隔',['Az09,Az09,Az09']),
      ('给出完整Python函数square(n)返回n乘自身，不额外打印',['def square','return']),
      ('确认收到即可，不附加问题',[]),
      ('请反驳这个绝对判断：所有人喜欢的音乐必定相同',[]),
    ]
    for i,(text,required) in enumerate(controls,1):
        add(f'V2C{i:02}','control',None,f'p{(i-1)%8}',[text],[],[],required_substrings=required,
            invariant='事实、完整任务、代码与用户明确要求；风格不同不算错误')
    assert len(cases)==100
    return {'version':'m763-natural-voice-independent-v2','purpose':'whole_prompt_voice_and_disposition_increment',
        'seed':763202,'authors':authors,'cases':cases,'coverage':{'persona':40,'relationship':20,'information':20,'control':20},
        'notes':['全新人工场景；不得用旧冻结集调参。','B为原M7提示，C为v2软表达/采样与倾向整体增量，不能把收益仅归因倾向。',
                 '允许符合人物的低风险虚构个人小经历；禁止编造用户或共同经历、外部事实或现实承诺。']}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    raw=json.dumps(material(),ensure_ascii=False,sort_keys=True,indent=2).encode('utf-8')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('xb') as f:f.write(raw)
    print(json.dumps({'cases':100,'authors':8,'sha256':sha256(raw).hexdigest(),'model_calls':0}))
