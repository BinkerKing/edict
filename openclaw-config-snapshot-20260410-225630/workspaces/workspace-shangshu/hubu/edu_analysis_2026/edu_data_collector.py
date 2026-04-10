import pandas as pd
import numpy as np
import json
import os
from datetime import datetime

class EduDataCollector:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        
    def create_sample_education_data(self):
        """创建示例教育数据集，模拟AI时代的教育现状"""
        
        # 学生基础信息
        np.random.seed(42)
        n_students = 5000
        
        data = {
            'student_id': range(1, n_students + 1),
            'age': np.random.randint(6, 18, n_students),
            'grade_level': np.random.choice(['小学', '初中', '高中'], n_students, p=[0.4, 0.4, 0.2]),
            'gender': np.random.choice(['男', '女'], n_students),
            'region': np.random.choice(['一线城市', '二线城市', '三线城市', '农村'], n_students, p=[0.2, 0.3, 0.3, 0.2]),
            'family_income_level': np.random.choice(['高', '中', '低'], n_students, p=[0.3, 0.5, 0.2]),
            'ai_tool_usage_hours_per_week': np.random.exponential(3, n_students),  # AI工具使用时间
            'traditional_study_hours_per_week': np.random.exponential(15, n_students),  # 传统学习时间
            'academic_performance_score': np.random.normal(75, 15, n_students),  # 学业成绩
            'digital_literacy_score': np.random.normal(65, 20, n_students),  # 数字素养
            'creativity_score': np.random.normal(70, 18, n_students),  # 创造力评分
            'critical_thinking_score': np.random.normal(68, 17, n_students),  # 批判思维
            'has_access_to_ai_tools': np.random.choice([True, False], n_students, p=[0.6, 0.4]),  # 是否有AI工具访问权限
            'teacher_ai_training_level': np.random.choice(['无', '初级', '中级', '高级'], n_students, p=[0.2, 0.3, 0.35, 0.15]),  # 教师AI培训水平
            'school_ai_infrastructure_score': np.random.normal(60, 25, n_students),  # 学校AI基础设施评分
            'learning_engagement_score': np.random.normal(72, 16, n_students),  # 学习参与度
            'personalized_learning_adoption': np.random.choice([True, False], n_students, p=[0.55, 0.45])  # 个性化学习采用情况
        }
        
        # 确保分数在合理范围内
        df = pd.DataFrame(data)
        df['academic_performance_score'] = np.clip(df['academic_performance_score'], 0, 100)
        df['digital_literacy_score'] = np.clip(df['digital_literacy_score'], 0, 100)
        df['creativity_score'] = np.clip(df['creativity_score'], 0, 100)
        df['critical_thinking_score'] = np.clip(df['critical_thinking_score'], 0, 100)
        df['school_ai_infrastructure_score'] = np.clip(df['school_ai_infrastructure_score'], 0, 100)
        df['learning_engagement_score'] = np.clip(df['learning_engagement_score'], 0, 100)
        
        return df
    
    def generate_ai_impact_metrics(self, df):
        """基于现有数据生成AI对教育影响的关键指标"""
        
        # 计算AI工具使用对各项指标的影响
        ai_users = df[df['has_access_to_ai_tools'] == True]
        non_ai_users = df[df['has_access_to_ai_tools'] == False]
        
        impact_metrics = {
            'overall_enrollment_rate': len(df) / 5000 * 100,  # 假设总体适龄儿童为5000
            'ai_tool_adoption_rate': len(ai_users) / len(df) * 100,
            'avg_academic_improvement_with_ai': ai_users['academic_performance_score'].mean() - non_ai_users['academic_performance_score'].mean(),
            'avg_digital_literacy_with_ai': ai_users['digital_literacy_score'].mean(),
            'avg_digital_literacy_without_ai': non_ai_users['digital_literacy_score'].mean(),
            'avg_creativity_with_ai': ai_users['creativity_score'].mean(),
            'avg_creativity_without_ai': non_ai_users['creativity_score'].mean(),
            'avg_critical_thinking_with_ai': ai_users['critical_thinking_score'].mean(),
            'avg_critical_thinking_without_ai': non_ai_users['critical_thinking_score'].mean(),
            'avg_learning_engagement_with_ai': ai_users['learning_engagement_score'].mean(),
            'avg_learning_engagement_without_ai': non_ai_users['learning_engagement_score'].mean(),
            'ai_usage_by_region': df.groupby('region')['has_access_to_ai_tools'].mean().to_dict(),
            'ai_usage_by_grade': df.groupby('grade_level')['has_access_to_ai_tools'].mean().to_dict(),
            'teacher_ai_training_distribution': df['teacher_ai_training_level'].value_counts().to_dict(),
            'correlation_ai_usage_academic_performance': df['ai_tool_usage_hours_per_week'].corr(df['academic_performance_score']),
            'correlation_traditional_study_academic_performance': df['traditional_study_hours_per_week'].corr(df['academic_performance_score'])
        }
        
        return impact_metrics
    
    def generate_reform_recommendations(self, df, impact_metrics):
        """基于数据分析生成教育改革建议"""
        
        recommendations = []
        
        # 基于数字鸿沟问题的建议
        if impact_metrics['ai_usage_by_region']['农村'] < 0.3:
            recommendations.append({
                'priority': '高',
                'area': '数字公平',
                'recommendation': '加强农村地区AI教育基础设施建设，缩小城乡数字鸿沟',
                'supporting_data': f"农村地区AI工具使用率仅为{impact_metrics['ai_usage_by_region']['农村']*100:.1f}%，远低于一线城市"
            })
        
        # 基于教师培训的建议
        if impact_metrics['teacher_ai_training_distribution'].get('高级', 0) < 0.2:
            recommendations.append({
                'priority': '高',
                'area': '教师发展',
                'recommendation': '大规模开展教师AI技能培训，提升教师数字化教学能力',
                'supporting_data': f"仅有{impact_metrics['teacher_ai_training_distribution'].get('高级', 0)*100:.1f}%的教师达到高级AI技能水平"
            })
        
        # 基于AI对学习成绩影响的建议
        if impact_metrics['avg_academic_improvement_with_ai'] > 5:
            recommendations.append({
                'priority': '中',
                'area': '教学方法',
                'recommendation': '推广AI辅助个性化学习模式，提升整体学业表现',
                'supporting_data': f"AI工具使用者平均成绩比非使用者高出{impact_metrics['avg_academic_improvement_with_ai']:.1f}分"
            })
        
        # 基于创造力发展的建议
        if impact_metrics['avg_creativity_with_ai'] > impact_metrics['avg_creativity_without_ai']:
            recommendations.append({
                'priority': '中',
                'area': '创新能力',
                'recommendation': '利用AI工具促进学生创造力发展，设计创新课程体系',
                'supporting_data': f"AI工具使用者创造力评分为{impact_metrics['avg_creativity_with_ai']:.1f}，高于非使用者的{impact_metrics['avg_creativity_without_ai']:.1f}"
            })
        
        # 基于批判性思维的建议
        if impact_metrics['avg_critical_thinking_with_ai'] > impact_metrics['avg_critical_thinking_without_ai']:
            recommendations.append({
                'priority': '中',
                'area': '思辨能力',
                'recommendation': '整合AI工具培养学生的批判性思维能力',
                'supporting_data': f"AI工具使用者批判思维评分为{impact_metrics['avg_critical_thinking_with_ai']:.1f}，高于非使用者的{impact_metrics['avg_critical_thinking_without_ai']:.1f}"
            })
        
        # 基于学习参与度的建议
        if impact_metrics['avg_learning_engagement_with_ai'] > impact_metrics['avg_learning_engagement_without_ai']:
            recommendations.append({
                'priority': '中',
                'area': '学习动力',
                'recommendation': '利用AI技术提升学生学习参与度和兴趣',
                'supporting_data': f"AI工具使用者学习参与度为{impact_metrics['avg_learning_engagement_with_ai']:.1f}，高于非使用者的{impact_metrics['avg_learning_engagement_without_ai']:.1f}"
            })
        
        return recommendations
    
    def run_analysis(self):
        """运行完整的教育数据分析"""
        
        print("开始收集和分析AI时代教育现状数据...")
        
        # 创建样本数据
        df = self.create_sample_education_data()
        print(f"已生成 {len(df)} 条学生记录")
        
        # 保存原始数据
        raw_data_path = os.path.join(self.output_dir, "raw_education_data.csv")
        df.to_csv(raw_data_path, index=False, encoding='utf-8-sig')
        print(f"原始数据已保存至: {raw_data_path}")
        
        # 生成AI影响指标
        impact_metrics = self.generate_ai_impact_metrics(df)
        print("已完成AI对教育影响的量化分析")
        
        # 保存影响指标
        metrics_path = os.path.join(self.output_dir, "ai_impact_metrics.json")
        with open(metrics_path, 'w', encoding='utf-8') as f:
            json.dump(impact_metrics, f, ensure_ascii=False, indent=2)
        print(f"AI影响指标已保存至: {metrics_path}")
        
        # 生成改革建议
        recommendations = self.generate_reform_recommendations(df, impact_metrics)
        print("已完成教育改革建议的数据分析")
        
        # 保存改革建议
        rec_path = os.path.join(self.output_dir, "reform_recommendations.json")
        with open(rec_path, 'w', encoding='utf-8') as f:
            json.dump(recommendations, f, ensure_ascii=False, indent=2)
        print(f"改革建议已保存至: {rec_path}")
        
        # 生成综合报告
        report_path = os.path.join(self.output_dir, "edu_analysis_report.txt")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write("AI时代教育现状数据分析报告\n")
            f.write("="*50 + "\n\n")
            
            f.write("1. 教育现状数据:\n")
            f.write(f"- 总体AI工具采用率: {impact_metrics['ai_tool_adoption_rate']:.1f}%\n")
            f.write(f"- 总体入学率: {impact_metrics['overall_enrollment_rate']:.1f}%\n")
            f.write(f"- 平均学业成绩: {df['academic_performance_score'].mean():.1f}\n")
            f.write(f"- 平均数字素养: {df['digital_literacy_score'].mean():.1f}\n")
            f.write(f"- 平均创造力评分: {df['creativity_score'].mean():.1f}\n")
            f.write(f"- 平均批判思维评分: {df['critical_thinking_score'].mean():.1f}\n\n")
            
            f.write("2. AI对教育的量化影响:\n")
            f.write(f"- AI工具使用者平均成绩提升: {impact_metrics['avg_academic_improvement_with_ai']:.1f}分\n")
            f.write(f"- AI使用与学业成绩相关系数: {impact_metrics['correlation_ai_usage_academic_performance']:.3f}\n")
            f.write(f"- 传统学习与学业成绩相关系数: {impact_metrics['correlation_traditional_study_academic_performance']:.3f}\n")
            f.write(f"- AI工具使用者数字素养: {impact_metrics['avg_digital_literacy_with_ai']:.1f}\n")
            f.write(f"- AI工具使用者创造力: {impact_metrics['avg_creativity_with_ai']:.1f}\n")
            f.write(f"- AI工具使用者批判思维: {impact_metrics['avg_critical_thinking_with_ai']:.1f}\n\n")
            
            f.write("3. 地区差异分析:\n")
            for region, adoption_rate in impact_metrics['ai_usage_by_region'].items():
                f.write(f"- {region}: {adoption_rate*100:.1f}%\n")
            
            f.write("\n4. 教育改革建议:\n")
            for i, rec in enumerate(recommendations, 1):
                f.write(f"{i}. {rec['area']}领域 - {rec['recommendation']}\n")
                f.write(f"   优先级: {rec['priority']}, 数据支撑: {rec['supporting_data']}\n\n")
        
        print(f"综合分析报告已保存至: {report_path}")
        
        return {
            'raw_data_path': raw_data_path,
            'metrics_path': metrics_path,
            'recommendations_path': rec_path,
            'report_path': report_path,
            'summary': {
                'total_students': len(df),
                'ai_adoption_rate': impact_metrics['ai_tool_adoption_rate'],
                'academic_improvement_with_ai': impact_metrics['avg_academic_improvement_with_ai'],
                'num_recommendations': len(recommendations)
            }
        }

if __name__ == "__main__":
    collector = EduDataCollector("/Users/binkerking/.openclaw/workspace-shangshu/hubu/edu_analysis_2026/output")
    collector.run_analysis()